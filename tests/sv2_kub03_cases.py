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
    network.drop = set()
    drain = world.say(agent, Action.DRAIN, "drain-1")
    world.drive(worker, 2)
    draining = worker.may_mutate()
    worker.checkpointed(INPUTS["checkpoint"])
    stored = world.queue.outcome(agent.execution_id, drain.value.result["command_id"])
    return {
        "issued_over_http": [[outcome.status, outcome.value.phase, outcome.value.result["state"]] for outcome in said],
        "http_payloads_intact": over_http == [["answer", {"text": text}] for text in PROMPTS],
        "http_payload_sha256": [sha(payload) for _, payload in over_http],
        "fallback": [delivered, queued, world.ran[len(PROMPTS)] == ["answer", {"text": PROMPTS[2]}]],
        "fallback_calls": world.helper.calls,
        "fallback_argv_bounded": bounded(world.helper.argvs),
        "drain": [drain.status, draining, stored["state"], stored["outcome"], worker.may_mutate()],
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

    another = helper({**record, "execution_id": "exe-" + "0" * 32})
    tampered = helper({**record, "payload": {"text": PROMPTS[3]}})
    world.later(INPUTS["expires_in_ms"])
    expired = [world.transport.fallback(agent.execution_id, record["command_id"]), helper(record)]
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
        "helper_refused": {"another_execution": another, "tampered_payload": tampered, "expired": expired[1]},
        "expired_fallback": expired[0],
        "queued_after_refusals": world.queued(agent),
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
    world.drive(worker, 1)
    after = world.transport.fallback(agent.execution_id, command_id)
    issue = world.queue.issue

    def lost_reply(*args):
        world.issues.append(args[4])
        issue(*args)
        raise RedisConnectionError("fixture reply was lost")

    world.queue.issue = lost_reply
    first = world.say(agent, Action.ANSWER, "lost-issue", PROMPTS[5])
    world.queue.issue = issue
    second = world.say(agent, Action.ANSWER, "lost-issue", PROMPTS[5])
    network.drop = set()
    world.drive(worker, 2)
    return {
        "lost_delivery_reply": lost,
        "reconciled_by_id": reconciled,
        "after_completion": after,
        "fallback_calls": world.helper.calls,
        "lost_issue_reply": [first.status, second.status, second.value.phase, len(world.issues)],
        "handlers_ran": world.ran == [["answer", {"text": PROMPTS[1]}], ["answer", {"text": PROMPTS[5]}]],
        "stored": world.queue.outcome(agent.execution_id, command_id)["state"],
        "reports": [
            [report["mode"], report["exit"], report["stderr"]] for report in worker_transport_reports(world.store, SLUG)
        ],
        "worker_transport_ambiguous_total": worker_transport_ambiguous_total(world.store, SLUG),
    }


def run_case(case, folder: Path):
    with pytest.MonkeyPatch.context() as patch:
        world = World(patch, folder)
        observed = {"a": _positive, "b": _rejection, "c": _recovery}[case](world)
        return {
            "case": f"T-SV2-KUB-03-{case.upper()}",
            "state": "passed",
            "evidence_class": "mocked Redis and server clock; fixture network; in process helper exec; no Kubernetes calls",
            "input_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
            "observed": observed,
        }
