import contextlib
import hashlib
import io
import json
import re
from pathlib import Path

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from scripts.swarm.store import AgentRecord
from scripts.swarm_v2.kubernetes.commands import (
    HELPER,
    KubernetesCommandTransport,
    worker_transport_ambiguous_total,
    worker_transport_reports,
)
from scripts.swarm_v2.kubernetes.runtime import GENERATION_LABEL
from scripts.swarm_v2.kubernetes.watch import BACKEND, EXECUTION_LABEL, OWNER_LABEL, owner_for
from scripts.swarm_v2.runtime.commands import Action, Commands, Principal, Request, Role
from scripts.swarm_v2.worker import control
from scripts.swarm_v2.worker.control import RouteTransport, WorkerControl, encode
from tests import sv2_ldg02_cases, sv2_ldg05_cases

FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/worker-command-prompts.json"
INPUTS = json.loads(FIXTURE.read_text(encoding="utf-8"))
PROMPTS = INPUTS["prompts"]
SLUG = sv2_ldg05_cases.SLUG
OPERATOR = "operator-fixture-credential"
ARGUMENT = re.compile(r"cmd-[0-9a-f]{32}|[A-Za-z0-9_-]+=*")
SECRET = "ghp" + "_" + "Ab1" * 12


class Pods:
    def __init__(self, namespace):
        self.namespace = namespace
        self.pods = {}

    def put(self, agent, **labels):
        metadata = {
            "name": f"swarm-{agent.execution_id}",
            "namespace": self.namespace,
            "uid": f"uid-{agent.execution_id}",
            "labels": {
                OWNER_LABEL: owner_for(SLUG),
                EXECUTION_LABEL: agent.execution_id,
                GENERATION_LABEL: str(agent.generation),
                **labels,
            },
        }
        self.pods[metadata["name"]] = {"metadata": metadata}

    def read_pod(self, name):
        return self.pods.get(name)


class Helper:
    """Runs the worker helper in process the way an exec would: argv only, never a shell; can lose the reply."""

    def __init__(self, world):
        self.world = world
        self.calls, self.argvs = [], []
        self.lose, self.drop = set(), set()
        self.stderr = b""

    def run(self, namespace, pod, argv):
        assert namespace == self.world.pods.namespace
        self.argvs.append(list(argv))
        self.calls.append([argv[3], len(argv)])
        if argv[3] in self.drop:
            raise ConnectionError("fixture exec was dropped")
        home = self.world.homes[pod]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = control.main(
                list(argv[3:]),
                inbox=home / "inbox",
                state=home / "worker.json",
                launch=home / "launch.json",
                clock=lambda: self.world.clock[0] / 1000,
            )
        if argv[3] in self.lose:
            raise TimeoutError("fixture exec reply was lost")
        return code, out.getvalue().encode(), self.stderr


class World(sv2_ldg05_cases.World):
    def __init__(self, monkeypatch, folder: Path):
        super().__init__(monkeypatch, folder)
        self.pods = Pods(INPUTS["namespace"])
        self.helper = Helper(self)
        self.homes = {}
        self.issues = []
        self.transport = self.restart_transport()

    def restart_transport(self):
        self.transport = KubernetesCommandTransport(self.queue, self.pods, self.helper, INPUTS["expires_in_ms"])
        return self.transport

    def commands(self):
        principal = Principal("operator", Role.OPERATOR)
        return Commands(
            self.store, [self.transport], lambda slug, credential: principal if credential == OPERATOR else None
        )

    def start(self, seat=sv2_ldg02_cases.INPUTS["seat"], task=sv2_ldg02_cases.INPUTS["task"], previous=""):
        target = {"pod_namespace": INPUTS["namespace"], "pod_name": "swarm-pending"}
        record = AgentRecord(
            self.store.next_name(SLUG, "eng"), "eng", task, seat=seat, runtime_backend=BACKEND, runtime_target=target
        )
        agent = self.controller.admit(record, previous)
        token = self.grants.issue(
            SLUG,
            agent.execution_id,
            project_ids=[sv2_ldg02_cases.INPUTS["project"]],
            brain_id="swarm",
            account="fixture",
        )
        self.pods.put(agent)
        return agent, token

    def control(self, agent, network, name="worker"):
        home = self.folder / f"swarm-{agent.execution_id}"
        home.mkdir(exist_ok=True)
        (home / "launch.json").write_text(
            json.dumps({"execution_id": agent.execution_id, "generation": agent.generation})
        )
        self.homes[f"swarm-{agent.execution_id}"] = home
        handlers = {kind: self.handler(kind) for kind in ("answer", "cancel", "stop")}
        transport = RouteTransport(agent.execution_id, network.send)
        return WorkerControl(transport, home / f"{name}.json", handlers, home / "inbox")

    def say(self, agent, action, key, text="", credential=OPERATOR):
        request = Request(agent.execution_id, agent.generation, action, key, text)
        return self.commands().execute(SLUG, request, credential)

    def drive(self, control, rounds=8):
        for _ in range(rounds):
            self.later(INPUTS["poll_interval_ms"])
            control.step()

    def queued(self, agent):
        return sorted(path.name for path in (self.homes[f"swarm-{agent.execution_id}"] / "inbox").glob("*"))


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def bounded(argvs):
    return all(argv[:3] == list(HELPER) and len(argv) == 5 and ARGUMENT.fullmatch(argv[4]) for argv in argvs)


def _positive(world):
    agent, network, worker = world.worker()
    said = [world.say(agent, Action.ANSWER, f"answer-{n}", text) for n, text in enumerate(PROMPTS)]
    world.drive(worker)
    over_http = list(world.ran)
    network.drop = {"commands"}
    fallback = world.say(agent, Action.ANSWER, "answer-fallback", PROMPTS[2])
    delivered = world.transport.fallback(agent.execution_id, fallback.value.result["command_id"])
    queued = world.queued(agent)
    world.drive(worker, 1)
    drain = world.say(agent, Action.DRAIN, "drain-fallback")
    drain_id = drain.value.result["command_id"]
    drain_delivered = world.transport.fallback(agent.execution_id, drain_id)
    world.drive(worker, 1)
    held = worker.records[drain_id]
    draining = worker.may_mutate()
    worker.checkpointed(INPUTS["checkpoint"])
    stored = world.queue.outcome(agent.execution_id, drain_id)
    other, _, other_worker = world.worker(sv2_ldg02_cases.INPUTS["other_seat"], sv2_ldg02_cases.INPUTS["other_task"])
    http_drain = world.say(other, Action.DRAIN, "drain-http")
    world.drive(other_worker, 2)
    http_draining = other_worker.may_mutate()
    other_worker.checkpointed(INPUTS["checkpoint"])
    http_stored = world.queue.outcome(other.execution_id, http_drain.value.result["command_id"])
    return {
        "issued_over_http": [[outcome.status, outcome.value.phase, outcome.value.result["state"]] for outcome in said],
        "http_payloads_intact": over_http == [["answer", {"text": text}] for text in PROMPTS],
        "http_payload_sha256": [sha(payload) for _, payload in over_http],
        "fallback": [delivered, len(queued), world.ran[len(PROMPTS)] == ["answer", {"text": PROMPTS[2]}]],
        "fallback_drain": [
            drain_delivered,
            held["kind"],
            held["payload"],
            draining,
            stored["state"],
            stored["outcome"],
        ],
        "http_drain": [http_drain.status, http_draining, http_stored["state"], http_stored["outcome"]],
        "fallback_calls": world.helper.calls,
        "fallback_argv_bounded": bounded(world.helper.argvs),
        "handlers_ran": len(world.ran),
        "worker_transport_ambiguous_total": worker_transport_ambiguous_total(world.store, SLUG),
    }


def _rejection(world):
    agent, network, worker = world.worker()
    network.drop = {"commands"}
    said = [world.say(agent, Action.ANSWER, f"meta-{n}", text) for n, text in enumerate(PROMPTS)]
    results = []
    for outcome in said:
        results.append(world.transport.fallback(agent.execution_id, outcome.value.result["command_id"]))
        world.drive(worker, 1)
    metacharacters = [world.ran == [["answer", {"text": text}] for text in PROMPTS], bounded(world.helper.argvs)]
    record = world.issue(agent, "answer", "foreign", {"text": PROMPTS[0]})

    def snapshot():
        commands = world.store.redis.hgetall(world.queue.key(agent.execution_id))
        return [commands, list(world.helper.calls), worker_transport_reports(world.store, SLUG)]

    before = snapshot()
    world.pods.put(agent, **{OWNER_LABEL: "agentihooks-swarm-other"})
    foreign_owner = world.transport.fallback(agent.execution_id, record["command_id"])
    world.pods.put(agent, **{GENERATION_LABEL: str(agent.generation + 1)})
    stale_label = world.transport.fallback(agent.execution_id, record["command_id"])
    world.pods.put(agent)
    unauthenticated = world.say(agent, Action.ANSWER, "anonymous", PROMPTS[2], credential="forged")
    unchanged = snapshot() == before
    home = world.homes[f"swarm-{agent.execution_id}"]

    def helper(record):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = control.main(
                ["deliver", encode(record)],
                inbox=home / "inbox",
                state=home / "worker.json",
                launch=home / "launch.json",
                clock=lambda: world.clock[0] / 1000,
            )
        return [code, json.loads(out.getvalue())["reason"]]

    refused = {
        "another_execution": helper({**record, "execution_id": "exe-" + "0" * 32}),
        "another_generation": helper({**record, "generation": record["generation"] + 1}),
        "tampered_payload": helper({**record, "payload": {"text": PROMPTS[3]}}),
    }
    queued_after_refusals = world.queued(agent)
    corrected = world.say(agent, Action.ANSWER, "corrected", PROMPTS[3])
    corrected_result = world.transport.fallback(agent.execution_id, corrected.value.result["command_id"])
    world.drive(worker, 1)
    corrected_ran = world.ran[-1] == ["answer", {"text": PROMPTS[3]}]
    world.later(INPUTS["expires_in_ms"])
    expired = world.transport.fallback(agent.execution_id, record["command_id"])
    refused["expired"] = helper(record)
    world.transport.fallback_enabled = False
    network.drop = set()
    rollback = world.say(agent, Action.ANSWER, "after-rollback", PROMPTS[4])
    disabled = world.transport.fallback(agent.execution_id, rollback.value.result["command_id"])
    world.drive(worker, 2)
    return {
        "fallback_results": results,
        "metacharacters_stayed_content": metacharacters,
        "foreign_owner_pod": foreign_owner,
        "stale_generation_pod": stale_label,
        "unauthenticated": [unauthenticated.status, unauthenticated.detail],
        "refusals_left_state_unchanged": unchanged,
        "helper_refused": refused,
        "queued_after_refusals": queued_after_refusals,
        "corrected_request": [corrected_result, corrected_ran],
        "expired_fallback": expired,
        "rollback": [disabled, world.ran[-1] == ["answer", {"text": PROMPTS[4]}]],
        "handlers_ran": len(world.ran),
        "worker_transport_ambiguous_total": worker_transport_ambiguous_total(world.store, SLUG),
    }


def _recovery(world):
    agent, network, worker = world.worker()
    network.drop = {"commands"}
    said = world.say(agent, Action.ANSWER, "uncertain", PROMPTS[1])
    command_id = said.value.result["command_id"]
    world.helper.lose = {"deliver"}
    lost = world.transport.fallback(agent.execution_id, command_id)
    world.helper.lose = set()
    world.restart_transport()
    world.helper.stderr = INPUTS["helper_stderr"].encode() + SECRET.encode()
    reconciled = world.transport.fallback(agent.execution_id, command_id)
    world.helper.stderr = b""
    network.lose = {"complete"}
    world.drive(worker, 1)
    network.lose = set()
    before_restart = [worker.records[command_id]["state"], len(world.ran)]
    restarted = world.control(agent, network)
    world.drive(restarted, 1)
    after_restart = [restarted.records[command_id]["state"], len(world.ran)]
    after = world.transport.fallback(agent.execution_id, command_id)
    issue = world.queue.issue

    def lost_reply(*args):
        world.issues.append(args[4])
        result = issue(*args)
        if len(world.issues) == 1:
            raise RedisConnectionError("fixture reply was lost")
        return result

    world.queue.issue = lost_reply
    first = world.say(agent, Action.ANSWER, "lost-issue", PROMPTS[5])
    second = world.say(agent, Action.ANSWER, "lost-issue", PROMPTS[5])
    world.queue.issue = issue
    stored_texts = [
        json.loads(raw)["payload"].get("text") for raw in world.store.redis.hvals(world.queue.key(agent.execution_id))
    ]
    network.drop = set()
    world.drive(restarted, 2)
    stale_drain = world.issue(agent, "drain", "drain-before-succession")["command_id"]
    world.drive(restarted, 1)
    world.later(sv2_ldg02_cases.INPUTS["lease_ms"])
    old = world.issue(agent, "answer", "before-succession", {"text": PROMPTS[0]})
    world.worker(previous=agent.execution_id)
    superseded = world.say(agent, Action.ANSWER, "after-succession", PROMPTS[0])
    restarted.checkpointed(INPUTS["checkpoint"])
    stale_checkpoint = [
        restarted.records[stale_drain]["state"],
        restarted.records[stale_drain].get("refusal"),
        world.queue.outcome(agent.execution_id, stale_drain)["state"],
    ]
    return {
        "lost_delivery_reply": lost,
        "reconciled_by_id": reconciled,
        "worker_restart": [before_restart, after_restart],
        "after_completion": after,
        "fallback_calls": world.helper.calls,
        "lost_issue_reply": [first.status, second.status, second.value.phase, len(world.issues)],
        "stored_lost_issue_commands": stored_texts.count(PROMPTS[5]),
        "handlers_ran": world.ran == [["answer", {"text": PROMPTS[1]}], ["answer", {"text": PROMPTS[5]}]],
        "stored": world.queue.outcome(agent.execution_id, command_id)["state"],
        "superseded": [
            world.transport.fallback(agent.execution_id, old["command_id"]),
            superseded.status,
            superseded.detail,
        ],
        "stale_checkpoint": stale_checkpoint,
        "reports": [
            [report["mode"], report["exit"], report["stderr"]] for report in worker_transport_reports(world.store, SLUG)
        ],
        "worker_transport_ambiguous_total": worker_transport_ambiguous_total(world.store, SLUG),
    }


def _held(observed):
    case = observed["case"]
    found = observed["observed"]
    if case == "a":
        return [
            found["http_payloads_intact"],
            found["fallback"] == ["queued", 1, True],
            found["fallback_drain"][:4] == ["queued", "drain", {}, False],
            found["fallback_drain"][4] == "completed",
            found["http_drain"][1:3] == [False, "completed"],
            found["fallback_argv_bounded"],
            found["worker_transport_ambiguous_total"] == {"status": 0, "deliver": 0},
        ]
    if case == "b":
        return [
            found["fallback_results"] == ["queued"] * len(PROMPTS),
            found["metacharacters_stayed_content"] == [True, True],
            [found["foreign_owner_pod"], found["stale_generation_pod"], found["unauthenticated"][0]] == ["refused"] * 3,
            found["refusals_left_state_unchanged"],
            all(code == 2 for code, _ in found["helper_refused"].values()),
            found["queued_after_refusals"] == [],
            found["corrected_request"] == ["queued", True],
            found["expired_fallback"] == "expired",
            found["rollback"] == ["disabled", True],
        ]
    return [
        [found["lost_delivery_reply"], found["reconciled_by_id"], found["after_completion"]]
        == ["ambiguous", "queued", "completed"],
        [mode for mode, _ in found["fallback_calls"]] == ["status", "deliver", "status"],
        found["worker_restart"][0][1] == found["worker_restart"][1][1] == 1,
        found["worker_restart"][1][0] == "reported",
        found["lost_issue_reply"] == ["ambiguous", "ok", "applied", 1],
        found["stored_lost_issue_commands"] == 1,
        found["handlers_ran"],
        found["superseded"][:2] == ["stale", "refused"],
        found["stale_checkpoint"] == ["rejected", "stale_generation", "accepted"],
        found["worker_transport_ambiguous_total"] == {"status": 0, "deliver": 1},
    ]


def run_case(case, folder: Path):
    with pytest.MonkeyPatch.context() as patch:
        world = World(patch, folder)
        observed = {"a": _positive, "b": _rejection, "c": _recovery}[case](world)
        held = _held({"case": case, "observed": observed})
        return {
            "case": f"T-SV2-KUB-03-{case.upper()}",
            "state": "passed" if all(held) else "failed",
            "checks": held,
            "evidence_class": "mocked Redis and server clock; fixture network; in process helper exec; no Kubernetes calls",
            "input_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
            "observed": observed,
        }
