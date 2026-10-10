import hashlib
import json
from pathlib import Path
from unittest import mock

import yaml

from scripts.swarm import lease
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm_v2 import control_service
from scripts.swarm_v2.auth_context import LaunchKey
from scripts.swarm_v2.kubernetes.watch import BACKEND
from scripts.swarm_v2.runtime import observe

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "swarm_v2"
VALUES = ROOT / "deploy" / "helm" / "agentihooks-swarm" / "values.yaml"
INPUTS = ("tests/fixtures/swarm_v2/control-nodes.json", "deploy/helm/agentihooks-swarm/values.yaml")
SLUG = "control-fixture"
OWNER = "hive-anton"
KEY = LaunchKey("launch-1", b"k" * 32)
START_MS = 1_000_000
EVIDENCE_CLASS = (
    "mocked: fakeredis store shared by both controller processes, a fake lease clock, the chart's controller "
    "affinity evaluated against labelled node fixtures; no Kubernetes scheduler, autoscaler or EC2 call was made"
)


class World:
    def __init__(self) -> None:
        import fakeredis

        self.server = fakeredis.FakeServer()
        self.clock = [START_MS]
        self.store().create(SwarmConfig(SLUG, "agentihooks", 2, 0))

    def store(self) -> RedisStore:
        import fakeredis

        return RedisStore(fakeredis.FakeRedis(server=self.server, decode_responses=True))

    def clocked(self):
        return mock.patch.object(lease, "now_ms", lambda store: self.clock[0])

    def service(self, authorize=lambda: True) -> control_service.ControlService:
        return control_service.ControlService(self.store(), SLUG, KEY, authorize, owner=OWNER)


def nodes() -> dict:
    return json.loads((FIXTURES / "control-nodes.json").read_text())


def controller_placement() -> dict:
    values = yaml.safe_load(VALUES.read_text())["controller"]
    return {"affinity": values["affinity"], "tolerations": values["tolerations"]}


def _matches(expression: dict, labels: dict) -> bool:
    key, operator = expression["key"], expression["operator"]
    return {
        "DoesNotExist": lambda: key not in labels,
        "Exists": lambda: key in labels,
        "In": lambda: labels.get(key) in expression["values"],
        "NotIn": lambda: labels.get(key) not in expression["values"],
    }[operator]()


def _affinity(placement: dict, node: dict) -> bool:
    required = placement["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]
    terms = required["nodeSelectorTerms"]
    return any(all(_matches(e, node["labels"]) for e in term["matchExpressions"]) for term in terms)


def _tolerated(placement: dict, node: dict) -> bool:
    tolerations = placement["tolerations"]
    return all(
        any(t.get("key") == taint["key"] and t.get("effect") in (None, taint["effect"]) for t in tolerations)
        for taint in node["taints"]
        if taint["effect"] == "NoSchedule"
    )


def placement(node: dict) -> dict:
    rules = controller_placement()
    return {
        "affinity_admits": _affinity(rules, node),
        "taints_admit": _tolerated(rules, node),
        "schedulable": _affinity(rules, node) and _tolerated(rules, node),
        "free_cpu_millicores": node["free_cpu_millicores"],
    }


def _admit(service, store, seat: str, task: str):
    pod = {"pod_namespace": "agentihooks-swarm-workers", "pod_name": f"swarm-{task}"}
    record = AgentRecord(
        store.next_name(SLUG, "eng"), "eng", task, seat=seat, runtime_backend=BACKEND, runtime_target=pod
    )
    agent = service.controller.admit(record, "")
    token = service.grants.issue(
        SLUG,
        agent.execution_id,
        project_ids=["github.com/the-cloud-clockwork/agentihooks"],
        brain_id="swarm",
        account="fixture",
    )
    service.executions.register(token, {"execution_id": agent.execution_id, "generation": agent.generation})
    return agent, token


def _beat(service, agent, sequence: int, epoch: int) -> dict:
    return {
        "schema_version": "2.1",
        "operation_id": f"heartbeat-{sequence}",
        "authority": {
            "execution_id": agent.execution_id,
            "task_id": agent.task,
            "task_generation": service.tasks.current(agent.task).generation,
            "controller_epoch": epoch,
            "owner_identity": agent.name,
        },
        "renewal_sequence": sequence,
        "state": "working",
        "observed_at": "2026-10-10T18:00:00Z",
    }


def _claims(service, agents) -> dict:
    return {
        agent.task: {"holder": claim.holder, "generation": claim.generation, "state": claim.state}
        for agent in agents
        for claim in [service.tasks.current(agent.task)]
    }


def _observed(store, agents) -> dict:
    return {agent.task: observe.stored(store, SLUG, agent.execution_id).state.value for agent in agents}


def _two_attempts(world: World):
    first = world.service()
    assert first.start()
    store = first.controller.store
    attempts = [_admit(first, store, f"eng-{n}@{SLUG}", f"t{n}") for n in (1, 2)]
    for agent, token in attempts:
        first.executions.heartbeat(agent.execution_id, token, _beat(first, agent, 1, first.controller.held.epoch))
    return first, attempts


def _positive() -> tuple[dict, bool]:
    durable = {node["name"]: placement(node) for node in nodes()["durable"]}
    runs = []
    for _ in range(2):
        world = World()
        with world.clocked():
            first, attempts = _two_attempts(world)
            agents = [agent for agent, _ in attempts]
            before = _claims(first, agents)
            first.stop()
            restart_ms = world.clock[0]
            second = world.service()
            restarted = second.start()
            world.clock[0] += 1000
            for agent, token in attempts:
                second.executions.heartbeat(
                    agent.execution_id, token, _beat(second, agent, 2, second.controller.held.epoch)
                )
            second.tick(now=world.clock[0] / 1000)
            runs.append(
                {
                    "restarted": restarted,
                    "epoch_after_graceful_stop": second.controller.held.epoch,
                    "task_authority_kept": _claims(second, agents) == before,
                    "observed": _observed(second.controller.store, agents),
                    "swarm_control_restart_recovery_seconds": (world.clock[0] - restart_ms) / 1000,
                }
            )
    observed = {"placement": durable, "runs": runs, "independent_runs_agree": runs[0] == runs[1]}
    passed = all(p["schedulable"] for p in durable.values()) and all(
        r["restarted"] and r["task_authority_kept"] and set(r["observed"].values()) == {"working"} for r in runs
    )
    return observed, passed and runs[0] == runs[1]


def _rejection() -> tuple[dict, bool]:
    reclaimable = {node["name"]: placement(node) for node in nodes()["reclaimable"]}
    world = World()
    store = world.store()
    forged = world.service(authorize=lambda: False)
    before = {"lease": lease.current(store, SLUG), "occupants": len(store.execution_occupants(SLUG))}
    try:
        forged.start()
        refusal = ""
    except SwarmError as error:
        refusal = str(error)
    after = {"lease": lease.current(store, SLUG), "occupants": len(store.execution_occupants(SLUG))}
    observed = {
        "placement": reclaimable,
        "forged_credential_refusal": refusal,
        "protected_state_unchanged": before == after,
        "refusal_discloses_no_key": KEY.secret.decode() not in refusal,
    }
    passed = (
        not any(p["schedulable"] or p["affinity_admits"] for p in reclaimable.values())
        and refusal == "a scoped controller grant is required"
        and before == after
    )
    return observed, passed


def _recovery() -> tuple[dict, bool]:
    world = World()
    with world.clocked():
        first, attempts = _two_attempts(world)
        agents = [agent for agent, _ in attempts]
        before = _claims(first, agents)
        epoch = first.controller.held.epoch
        world.clock[0] += 5000
        restart_ms = world.clock[0]
        second = world.service()
        restarted = second.start()
        rival = control_service.ControlService(world.store(), SLUG, KEY, lambda: True, owner="hive-rival")
        rival_took = rival.start()
        world.clock[0] += 2000
        acks, stale = {}, {}
        for agent, token in attempts:
            body = _beat(second, agent, 2, second.controller.held.epoch)
            first_ack = second.executions.heartbeat(agent.execution_id, token, body)
            replay_ack = second.executions.heartbeat(agent.execution_id, token, body)
            acks[agent.task] = {"epoch": first_ack["controller_epoch"], "replay_identical": first_ack == replay_ack}
            older = {**_beat(second, agent, 1, epoch), "state": "starting"}
            try:
                second.executions.heartbeat(agent.execution_id, token, older)
                stale[agent.task] = "accepted"
            except Exception as error:
                stale[agent.task] = getattr(error, "error_class", type(error).__name__)
        second.tick(now=world.clock[0] / 1000)
        store = second.controller.store
        observed = {
            "restarted": restarted,
            "epochs": [epoch, second.controller.held.epoch],
            "rival_controller_took_lease": rival_took,
            "active_attempts_after_restart": sorted(a.task for a in store.execution_occupants(SLUG).values()),
            "task_authority_kept": _claims(second, agents) == before,
            "current_heartbeats": acks,
            "older_heartbeats": stale,
            "observed": _observed(store, agents),
            "swarm_control_restart_recovery_seconds": (world.clock[0] - restart_ms) / 1000,
        }
    passed = (
        restarted
        and not rival_took
        and observed["epochs"] == [epoch, epoch]
        and observed["active_attempts_after_restart"] == ["t1", "t2"]
        and observed["task_authority_kept"]
        and "accepted" not in stale.values()
        and all(ack["epoch"] == epoch and ack["replay_identical"] for ack in acks.values())
        and set(observed["observed"].values()) == {"working"}
    )
    return observed, passed


def run_case(case: str) -> dict:
    observed, passed = {"a": _positive, "b": _rejection, "c": _recovery}[case]()
    return {
        "case": f"T-SV2-GIT-01-{case.upper()}",
        "state": "passed" if passed else "failed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in INPUTS},
        "observed": observed,
    }
