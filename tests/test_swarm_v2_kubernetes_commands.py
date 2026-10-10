import json
import subprocess
from pathlib import Path

import pytest
from redis.exceptions import RedisError

from scripts.swarm_v2.auth_context import GrantRefused
from scripts.swarm_v2.kubernetes import commands
from scripts.swarm_v2.kubernetes.client import ApiRefused
from scripts.swarm_v2.kubernetes.commands import KubectlExec, KubernetesCommandTransport, sanitize_report
from scripts.swarm_v2.kubernetes.runtime import GENERATION_LABEL
from scripts.swarm_v2.kubernetes.watch import EXECUTION_LABEL, OWNER_LABEL
from scripts.swarm_v2.runtime.commands import Action
from scripts.swarm_v2.runtime.operations import Observation, Operation, Phase, digest
from scripts.swarm_v2.worker.control import encode

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-KUB-03"
TEXT = "$(rm -rf /) `id` \"q\" 'q'\nnext line ü"
SECRET = "ghp" + "_" + "Ab1" * 12
REPLACEMENT = chr(0xFFFD)


@pytest.fixture
def world(monkeypatch, tmp_path):
    from tests.sv2_kub03_cases import World

    return World(monkeypatch, tmp_path)


@pytest.fixture
def agent(world):
    agent, _ = world.start()
    return agent


class Runner:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def run(self, namespace, pod, argv):
        self.calls.append([namespace, pod, list(argv)])
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def reply(record, state, code=0, **changes):
    ids = {name: record[name] for name in ("command_id", "execution_id", "generation")}
    return code, json.dumps({**ids, "state": state, **changes}).encode(), b""


def operation(agent, action="command", payload=None, generation=None, key="op-key"):
    payload = {"command": "answer", "text": TEXT} if payload is None else payload
    return Operation(
        key,
        agent.execution_id,
        agent.generation if generation is None else generation,
        action,
        "kubernetes",
        digest({"action": action, "payload": payload}),
        {},
    )


def transport(world, runner=None, enabled=True):
    return KubernetesCommandTransport(world.queue, world.pods, runner or Runner(), 100, enabled)


def issued(world, agent, key="cmd-key", text=TEXT):
    return world.queue.issue(agent.execution_id, agent.generation, "answer", {"text": text}, key, 100)


def ambiguous(world):
    return commands.worker_transport_ambiguous_total(world.store, world.queue.slug)


def reports(world):
    return commands.worker_transport_reports(world.store, world.queue.slug)


def test_the_transport_names_its_backend_helper_and_bounds():
    assert KubernetesCommandTransport.backend == "kubernetes"
    assert KubernetesCommandTransport.commands == frozenset((Action.ANSWER, Action.DRAIN, Action.CANCEL))
    assert commands.HELPER == ("python", "-m", "scripts.swarm_v2.worker.control")
    assert commands.CONTAINER == "agent"
    assert commands.MODES == ("status", "deliver")
    assert commands.IDENTITY == ("command_id", "execution_id", "generation")
    assert commands.REPLIES == frozenset(("queued", "known", "absent", "refused"))
    assert (commands.EXEC_TIMEOUT_SECONDS, commands.REPORT_CHARS, commands.REPORTS_KEPT) == (10, 2048, 200)
    assert (commands.ENVELOPE_CHARS, commands.RAW_BYTES) == (100_000, 65536)


def test_sanitize_report_strips_escape_and_control_sequences_but_keeps_lines_and_tabs():
    raw = b"\x1b[31mred\x1b[0m \x1b]0;title\x07osc \x1b]2;t\x1b\\st \x1bDfe\x00\x07\x08\r\x0b\x0c\x7f\xc2\x9b2J a\tb\nc"
    assert sanitize_report(raw) == "red osc st fe a\tb\nc"
    assert sanitize_report(b"a\x1fb\x1bZc\x1b@d\xc2\x9b1;2me\x1b[?25lf") == "abcdef"
    strings = (
        b"g\x1bPq#1\x1b\\h\xc2\x9dtitle\x07i\xc2\x90dcs\xc2\x9cj\xc2\x9fapc\xc2\x9ck\x1b_pm\x1b\\l\xc2\x98sos\xc2\x9cm"
    )
    assert sanitize_report(strings) == "ghijklm"
    assert sanitize_report(b"n\x1bXsos\x1b\\o\x1b^pm\x07p\xc2\x9epm\x07q\x1b]unterminated") == "nopq"
    assert sanitize_report(b"\xc2\xa0kept \xc2\xa1") == "\xa0kept \xa1"


def test_sanitize_report_replaces_invalid_utf8_and_redacts_secrets():
    expected = f"ok {REPLACEMENT} token [REDACTED:github_token] end"
    assert sanitize_report(b"ok \xff " + f"token {SECRET} end".encode()) == expected
    bearer = "Bea" + "rer " + "z9" * 12
    assert sanitize_report(f"auth {bearer} end".encode()) == "auth [REDACTED:bearer_value] end"


def test_sanitize_report_bounds_its_length_after_redaction():
    exact = b"x" * commands.REPORT_CHARS
    assert sanitize_report(exact) == exact.decode()
    assert sanitize_report(exact + b"y") == exact.decode() + "[truncated]"
    padded = SECRET.encode() + b" " + b"z" * (commands.REPORT_CHARS - 28)
    assert sanitize_report(padded) == "[REDACTED:github_token] " + "z" * (commands.REPORT_CHARS - 28)


def test_sanitize_report_withholds_an_oversized_output_whole():
    assert sanitize_report(b"x" * commands.RAW_BYTES) == "x" * commands.REPORT_CHARS + "[truncated]"
    assert sanitize_report(b"x" * (commands.RAW_BYTES + 1)) == f"[{commands.RAW_BYTES + 1} bytes withheld]"


def test_the_metrics_start_at_zero_for_every_helper_mode(world):
    assert ambiguous(world) == {"status": 0, "deliver": 0}
    assert reports(world) == []


def test_kubectl_exec_passes_argv_after_a_separator_and_never_a_shell():
    seen = []

    def runner(command, **options):
        seen.append([command, options])
        return subprocess.CompletedProcess(command, 3, b"out", b"err")

    argv = ("python", "-m", "scripts.swarm_v2.worker.control", "deliver", "$(id);`id`")
    assert KubectlExec("/bin/kubectl", runner).run("swarm", "swarm-exe", argv) == (3, b"out", b"err")
    assert seen == [
        [
            ["/bin/kubectl", "exec", "--namespace", "swarm", "swarm-exe", "--container", "agent", "--", *argv],
            {"capture_output": True, "timeout": 10, "check": False},
        ]
    ]


def test_kubectl_exec_defaults_to_kubectl_through_subprocess_run():
    default = KubectlExec()
    assert (default.kubectl, default.runner) == ("kubectl", subprocess.run)


@pytest.mark.parametrize(
    "error, raised, message",
    [
        (subprocess.TimeoutExpired(["kubectl"], 10), TimeoutError, "the helper exec timed out"),
        (FileNotFoundError("kubectl"), ConnectionError, "kubectl could not start"),
    ],
)
def test_kubectl_exec_reports_an_unsure_exec_as_a_transport_error(error, raised, message):
    def runner(command, **options):
        raise error

    with pytest.raises(raised) as caught:
        KubectlExec(runner=runner).run("swarm", "pod", ("python",))
    assert str(caught.value) == message
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__


def test_apply_issues_an_answer_into_the_command_queue_with_its_text_intact(world, agent):
    op = operation(agent)
    observed = transport(world).apply_operation(op, {"command": "answer", "text": TEXT})
    command_id = world.queue.command_id(agent.execution_id, op.operation_id)
    stored = world.queue.outcome(agent.execution_id, command_id)
    assert (stored["kind"], stored["payload"], stored["generation"]) == ("answer", {"text": TEXT}, agent.generation)
    assert stored["expires_at_ms"] == stored["issued_at_ms"] + 100
    assert observed == Observation(
        Phase.APPLIED,
        agent.execution_id,
        agent.generation,
        "kubernetes",
        op.payload_digest,
        {"command_id": command_id, "state": "issued", "expires_at_ms": stored["expires_at_ms"]},
    )


@pytest.mark.parametrize("command, action", [("drain", "drain"), ("cancel", "command")])
def test_apply_issues_drain_and_cancel_without_a_payload(world, agent, command, action):
    payload = {"command": command, "text": ""}
    op = operation(agent, action, payload)
    observed = transport(world).apply_operation(op, payload)
    stored = world.queue.outcome(agent.execution_id, world.queue.command_id(agent.execution_id, op.operation_id))
    assert (stored["kind"], stored["payload"]) == (command, {})
    assert observed.phase is Phase.APPLIED
    assert transport(world).observe_operation(op).phase is Phase.APPLIED


@pytest.mark.parametrize(
    "payload",
    [
        {"command": "detach", "text": ""},
        {"command": "force-stop", "text": ""},
        {"command": "stop", "text": ""},
        {"command": "answer"},
        {"command": "answer", "text": TEXT, "shell": "sh -c"},
        {"command": "drain", "text": "x"},
        {"command": "cancel", "text": "x"},
        {"text": TEXT},
    ],
)
def test_apply_refuses_any_command_outside_the_bounded_kinds_without_issuing(world, agent, payload):
    before = world.store.redis.hgetall(world.queue.key(agent.execution_id))
    assert transport(world).apply_operation(operation(agent, payload=payload), payload) == Observation(Phase.REFUSED)
    assert world.store.redis.hgetall(world.queue.key(agent.execution_id)) == before == {}


def test_apply_refuses_a_stale_generation_and_a_conflicting_key(world, agent):
    payload = {"command": "answer", "text": TEXT}
    stale = operation(agent, generation=agent.generation + 1)
    assert transport(world).apply_operation(stale, payload) == Observation(Phase.REFUSED)
    first = operation(agent)
    assert transport(world).apply_operation(first, payload).phase is Phase.APPLIED
    other = {"command": "answer", "text": "other"}
    assert transport(world).apply_operation(operation(agent, payload=other), other) == Observation(Phase.REFUSED)


def test_apply_is_unsure_when_the_queue_is_unavailable(world, agent, monkeypatch):
    payload = {"command": "answer", "text": TEXT}

    def contended(*args):
        raise GrantRefused("dependency_unavailable", "commands kept changing; nothing was recorded")

    monkeypatch.setattr(world.queue, "issue", contended)
    assert transport(world).apply_operation(operation(agent), payload) == Observation(Phase.UNKNOWN)

    def down(*args):
        raise RedisError("down")

    monkeypatch.setattr(world.queue, "issue", down)
    assert transport(world).apply_operation(operation(agent), payload) == Observation(Phase.UNKNOWN)


def test_observe_reconciles_by_command_id(world, agent, monkeypatch):
    payload = {"command": "answer", "text": TEXT}
    op = operation(agent)
    assert transport(world).observe_operation(op) == Observation(Phase.ABSENT)
    applied = transport(world).apply_operation(op, payload)
    assert transport(world).observe_operation(op) == applied
    monkeypatch.setattr(world.queue, "outcome", lambda *args: (_ for _ in ()).throw(RedisError("down")))
    assert transport(world).observe_operation(op) == Observation(Phase.UNKNOWN)


def test_observe_refuses_a_stored_command_that_differs_from_the_operation(world, agent):
    payload = {"command": "answer", "text": TEXT}
    transport(world).apply_operation(operation(agent), payload)
    changed = operation(agent, payload={"command": "answer", "text": "other"})
    assert transport(world).observe_operation(changed) == Observation(Phase.REFUSED)
    drained = operation(agent, "drain", {"command": "drain", "text": ""})
    assert transport(world).observe_operation(drained) == Observation(Phase.REFUSED)
    stale = operation(agent, generation=agent.generation + 1)
    assert transport(world).observe_operation(stale) == Observation(Phase.REFUSED)


def test_observe_refuses_a_command_stored_for_another_execution(world, agent):
    payload = {"command": "answer", "text": TEXT}
    op = operation(agent)
    transport(world).apply_operation(op, payload)
    key = world.queue.key(agent.execution_id)
    command_id = world.queue.command_id(agent.execution_id, op.operation_id)
    stored = json.loads(world.store.redis.hget(key, command_id))
    world.store.redis.hset(key, command_id, json.dumps({**stored, "execution_id": "exe-other"}))
    assert transport(world).observe_operation(op) == Observation(Phase.REFUSED)


def test_fallback_does_nothing_when_disabled_absent_or_no_longer_issued(world, agent):
    runner = Runner()
    command = issued(world, agent)
    assert transport(world, runner, enabled=False).fallback(agent.execution_id, command["command_id"]) == "disabled"
    assert transport(world, runner).fallback(agent.execution_id, "cmd-" + "0" * 32) == "absent"
    world.queue.accept(agent.execution_id, command["command_id"], command["payload_digest"])
    assert transport(world, runner).fallback(agent.execution_id, command["command_id"]) == "accepted"
    expired = issued(world, agent, "expiring")
    world.later(100)
    assert transport(world, runner).fallback(agent.execution_id, expired["command_id"]) == "expired"
    assert runner.calls == []


def test_fallback_never_reaches_a_superseded_execution(world, agent):
    command, runner = issued(world, agent), Runner()
    world.start(previous=agent.execution_id)
    assert transport(world, runner).fallback(agent.execution_id, command["command_id"]) == "stale"
    assert runner.calls == []


def test_fallback_leaves_an_oversized_envelope_to_the_endpoint(world, agent, monkeypatch):
    command = issued(world, agent)
    monkeypatch.setattr(commands, "ENVELOPE_CHARS", len(encode(command)) - 1)
    runner = Runner()
    assert transport(world, runner).fallback(agent.execution_id, command["command_id"]) == "too_large"
    assert runner.calls == []
    monkeypatch.setattr(commands, "ENVELOPE_CHARS", len(encode(command)))
    runner = Runner(reply(command, "known"))
    assert transport(world, runner).fallback(agent.execution_id, command["command_id"]) == "known"


def test_fallback_asks_the_status_first_then_delivers_the_same_envelope(world, agent):
    command = issued(world, agent)
    runner = Runner(reply(command, "absent"), reply(command, "queued"))
    assert transport(world, runner).fallback(agent.execution_id, command["command_id"]) == "queued"
    pod, namespace, envelope = f"swarm-{agent.execution_id}", world.pods.namespace, encode(command)
    assert runner.calls == [
        [namespace, pod, [*commands.HELPER, "status", envelope]],
        [namespace, pod, [*commands.HELPER, "deliver", envelope]],
    ]
    assert ambiguous(world) == {"status": 0, "deliver": 0}


@pytest.mark.parametrize("state", ["queued", "known"])
def test_fallback_never_redelivers_a_command_the_worker_already_holds(world, agent, state):
    command = issued(world, agent)
    runner = Runner(reply(command, state))
    assert transport(world, runner).fallback(agent.execution_id, command["command_id"]) == state
    assert [call[2][3] for call in runner.calls] == ["status"]


def test_fallback_passes_a_helper_refusal_through(world, agent):
    command = issued(world, agent)
    runner = Runner(reply(command, "absent"), reply(command, "refused", code=2, reason="the command expired"))
    assert transport(world, runner).fallback(agent.execution_id, command["command_id"]) == "refused"
    assert ambiguous(world) == {"status": 0, "deliver": 0}


@pytest.mark.parametrize(
    "answer",
    [
        "timeout",
        "dropped",
        "not json",
        "list",
        "other command",
        "other execution",
        "other generation",
        "state only",
        "unknown state",
        "refused with success",
        "queued with failure",
    ],
)
def test_fallback_counts_any_unsure_status_reply_and_delivers_nothing(world, agent, answer):
    command = issued(world, agent)
    answers = {
        "timeout": TimeoutError("lost"),
        "dropped": ConnectionError("dropped"),
        "not json": (0, b"not json", b""),
        "list": (0, b"[]", b""),
        "other command": reply({**command, "command_id": "cmd-" + "9" * 32}, "queued"),
        "other execution": reply({**command, "execution_id": "exe-other"}, "queued"),
        "other generation": reply({**command, "generation": command["generation"] + 1}, "queued"),
        "state only": (0, json.dumps({"state": "queued"}).encode(), b""),
        "unknown state": reply(command, "running"),
        "refused with success": reply(command, "refused"),
        "queued with failure": reply(command, "queued", code=1),
    }
    runner = Runner(answers[answer])
    assert transport(world, runner).fallback(agent.execution_id, command["command_id"]) == "ambiguous"
    assert ambiguous(world) == {"status": 1, "deliver": 0}
    assert len(runner.calls) == 1


def test_fallback_counts_a_lost_delivery_reply_and_reports_it(world, agent):
    command = issued(world, agent)
    runner = Runner(reply(command, "absent"), TimeoutError("lost"))
    assert transport(world, runner).fallback(agent.execution_id, command["command_id"]) == "ambiguous"
    assert ambiguous(world) == {"status": 0, "deliver": 1}
    base = {"execution_id": agent.execution_id, "generation": agent.generation, "command_id": command["command_id"]}
    assert reports(world) == [
        {**base, "mode": "status", "exit": 0, "stdout": reply(command, "absent")[1].decode(), "stderr": ""},
        {**base, "mode": "deliver", "exit": None, "stdout": "", "stderr": ""},
    ]


def test_fallback_sanitizes_helper_output_before_storing_it(world, agent):
    command = issued(world, agent)
    noisy = (2, b"\x1b[2Jgarbage " + SECRET.encode(), b"\x1b[31mtrace\x1b[0m\r\n" + b"e" * 3000)
    transport(world, Runner(noisy)).fallback(agent.execution_id, command["command_id"])
    [report] = reports(world)
    assert report["stdout"] == "garbage [REDACTED:github_token]"
    assert report["stderr"] == "trace\n" + "e" * (commands.REPORT_CHARS - 6) + "[truncated]"
    stored = world.store.redis.lrange(world.store.key(world.queue.slug, "worker-transport-reports"), 0, -1)
    assert SECRET not in json.dumps(stored)


def test_fallback_keeps_only_the_newest_reports(world, agent, monkeypatch):
    monkeypatch.setattr(commands, "REPORTS_KEPT", 2)
    for n in range(3):
        command = issued(world, agent, f"k{n}")
        transport(world, Runner(reply(command, "known"))).fallback(agent.execution_id, command["command_id"])
    assert [report["command_id"] for report in reports(world)] == [
        world.queue.command_id(agent.execution_id, key) for key in ("k1", "k2")
    ]


@pytest.mark.parametrize(
    "labels",
    [
        {OWNER_LABEL: "agentihooks-swarm-other"},
        {EXECUTION_LABEL: "exe-other"},
        {GENERATION_LABEL: "99"},
    ],
)
def test_fallback_execs_only_into_a_pod_carrying_this_executions_labels(world, agent, labels):
    command = issued(world, agent)
    world.pods.put(agent, **labels)
    runner = Runner()
    assert transport(world, runner).fallback(agent.execution_id, command["command_id"]) == "refused"
    assert runner.calls == []


def test_fallback_refuses_a_missing_unlabelled_or_deleting_pod(world, agent):
    command, runner = issued(world, agent), Runner()
    name = f"swarm-{agent.execution_id}"
    world.pods.pods[name]["metadata"]["deletionTimestamp"] = "2026-10-10T00:00:00Z"
    assert transport(world, runner).fallback(agent.execution_id, command["command_id"]) == "refused"
    del world.pods.pods[name]["metadata"]["labels"]
    del world.pods.pods[name]["metadata"]["deletionTimestamp"]
    assert transport(world, runner).fallback(agent.execution_id, command["command_id"]) == "refused"
    del world.pods.pods[name]
    assert transport(world, runner).fallback(agent.execution_id, command["command_id"]) == "refused"
    assert runner.calls == []


@pytest.mark.parametrize("error", [ApiRefused(403, "Forbidden"), ConnectionError("down"), TimeoutError("slow")])
def test_fallback_is_unavailable_when_the_pod_cannot_be_read(world, agent, monkeypatch, error):
    command, runner = issued(world, agent), Runner()

    def unreadable(name):
        raise error

    monkeypatch.setattr(world.pods, "read_pod", unreadable)
    assert transport(world, runner).fallback(agent.execution_id, command["command_id"]) == "unavailable"
    assert runner.calls == []
    assert world.queue.outcome(agent.execution_id, command["command_id"])["state"] == "issued"


def test_fallback_is_unavailable_while_the_store_is_down(world, agent, monkeypatch):
    command, runner = issued(world, agent), Runner()
    monkeypatch.setattr(world.queue, "outcome", lambda *args: (_ for _ in ()).throw(RedisError("down")))
    assert transport(world, runner).fallback(agent.execution_id, command["command_id"]) == "unavailable"
    assert runner.calls == []


def test_fallback_reads_the_pod_named_after_the_execution(world, agent, monkeypatch):
    command, names = issued(world, agent), []
    read = world.pods.read_pod
    monkeypatch.setattr(world.pods, "read_pod", lambda name: names.append(name) or read(name))
    transport(world, Runner(reply(command, "known"))).fallback(agent.execution_id, command["command_id"])
    assert names == [f"swarm-{agent.execution_id}"]


def test_rollback_disables_only_the_fallback_while_http_delivery_keeps_working(world):
    from tests.sv2_kub03_cases import PROMPTS

    agent, network, worker = world.worker()
    world.transport.fallback_enabled = False
    said = world.say(agent, Action.ANSWER, "rollback", PROMPTS[2])
    assert world.transport.fallback(agent.execution_id, said.value.result["command_id"]) == "disabled"
    world.drive(worker, 1)
    assert world.ran == [["answer", {"text": PROMPTS[2]}]]
    assert world.helper.calls == []


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case, tmp_path):
    from tests.sv2_kub03_cases import run_case

    first_folder, second_folder = tmp_path / "first", tmp_path / "second"
    first_folder.mkdir()
    second_folder.mkdir()
    first, second = run_case(case, first_folder), run_case(case, second_folder)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True, ensure_ascii=False)
    path = EVIDENCE / f"{case}-result.json"
    record = {"case": f"T-SV2-KUB-03-{case.upper()}", "independent_runs": 2, "observed": first}
    assert path.exists(), json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False)
    assert json.loads(path.read_text(encoding="utf-8")) == record, json.dumps(
        record, indent=2, sort_keys=True, ensure_ascii=False
    )


def test_the_manifest_names_the_hash_of_every_case_input():
    import hashlib

    manifest = json.loads((EVIDENCE / "manifest.json").read_text(encoding="utf-8"))
    root = Path(__file__).parents[1]
    assert manifest["cases"] == ["T-SV2-KUB-03-A", "T-SV2-KUB-03-B", "T-SV2-KUB-03-C"]
    fixture = "tests/fixtures/swarm_v2/worker-command-prompts.json"
    assert manifest["inputs"][fixture] == hashlib.sha256((root / fixture).read_bytes()).hexdigest()


def test_the_package_record_carries_its_completion_evidence():
    record = json.loads((EVIDENCE / "result.json").read_text(encoding="utf-8"))
    assert (record["package"], record["package_complete"]) == ("SV2-KUB-03", False)
    assert set(record["states"]) == {"observed", "accepted", "committed", "externally_verified"}
    for name in ("interfaces", "supported_versions", "authoritative_objects", "remaining_limitations", "validation"):
        assert record[name], name
    assert record["cases"] == {
        **{case: f"evidence/SV2-KUB-03/{case}-result.json" for case in "abc"},
        "manifest": "evidence/SV2-KUB-03/manifest.json",
    }
    assert set(record["measurements"]["worker_transport_ambiguous_total"]) == {"a", "b", "c"}
    path, name = record["rollback_rehearsal"]["test"].split("::")
    assert path == "tests/test_swarm_v2_kubernetes_commands.py"
    assert f"\ndef {name}(" in Path(__file__).read_text(encoding="utf-8")
    rejection = json.loads((EVIDENCE / "b-result.json").read_text(encoding="utf-8"))
    assert rejection["observed"]["observed"]["rollback"] == ["disabled", True]
