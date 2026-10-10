import hashlib
import json
from pathlib import Path

import fakeredis
import pytest

from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2.api.executions import ExecutionsAPI, heartbeat_rejections, heartbeat_rejections_total
from scripts.swarm_v2.auth_context import GrantRefused, LaunchAuthority, LaunchKey
from scripts.swarm_v2.authority import TaskAuthority
from scripts.swarm_v2.controller import Controller

FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/worker-heartbeat.json"
INPUTS = json.loads(FIXTURE.read_text(encoding="utf-8"))
SLUG = INPUTS["swarm"]


class World:
    def __init__(self, monkeypatch):
        self.store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
        self.store.create(SwarmConfig(SLUG, "agentihooks", 2, 0))
        self.clock = [INPUTS["clock_ms"]]
        monkeypatch.setattr("time.time", lambda: self.clock[0] / 1000)
        self.controller = Controller(self.store, SLUG, [], lambda: True)
        assert self.controller.acquire()
        self.grants = LaunchAuthority(
            self.store, LaunchKey("fixture-key", b"x" * 32), "controller", "workers", lambda: self.clock[0] / 1000
        )
        self.tasks = TaskAuthority(self.store, self.controller, lambda token: self.grants.bound(SLUG, token))
        self.api = ExecutionsAPI(self.grants, self.tasks, INPUTS["lease_ms"])

    def start(self, seat=INPUTS["seat"], task=INPUTS["task"], previous=""):
        record = AgentRecord(self.store.next_name(SLUG, "eng"), "eng", task, seat=seat)
        agent = self.controller.admit(record, previous)
        token = self.grants.issue(
            SLUG, agent.execution_id, project_ids=[INPUTS["project"]], brain_id="swarm", account="fixture"
        )
        return agent, token

    def register(self, agent, token):
        body = {"execution_id": agent.execution_id, "generation": agent.generation}
        return self.api.route("POST", "/v2/executions/register", f"Bearer {token}", body)

    def beat(self, agent, sequence, generation=1, **extra):
        return {
            "schema_version": "2.1",
            "operation_id": f"heartbeat-{sequence}",
            "authority": {
                "execution_id": agent.execution_id,
                "task_id": agent.task,
                "task_generation": generation,
                "controller_epoch": self.controller.held.epoch,
                "owner_identity": agent.name,
            },
            "renewal_sequence": sequence,
            "state": "working",
            "observed_at": "2026-10-08T08:05:00Z",
            **extra,
        }

    def command(self, agent, operation_id):
        operation = {
            "operation_id": operation_id,
            "execution_id": agent.execution_id,
            "generation": agent.generation,
            "action": "command",
            "backend": "local",
            "payload_digest": "0" * 64,
            "target": {},
            "phase": "accepted",
        }
        self.store.redis.hset(self.store.key(SLUG, "runtime-operations"), operation_id, json.dumps(operation))

    def put(self, execution_id, token, body):
        return self.api.route("PUT", f"/v2/executions/{execution_id}/heartbeat", f"Bearer {token}", body)

    def protected(self):
        redis = self.store.redis
        reads = {"string": redis.get, "hash": redis.hgetall, "list": lambda key: redis.lrange(key, 0, -1)}
        keys = sorted(key for key in redis.keys(f"*{SLUG}*") if not key.endswith("rejections"))
        return {key: reads.get(redis.type(key), redis.type)(key) for key in keys}


def stable(ack):
    return {name: ack[name] for name in sorted(ack) if name not in ("execution_id", "owner_identity", "grant_id")}


def _positive(world):
    agent, token = world.start()
    other, other_token = world.start(INPUTS["other_seat"], INPUTS["other_task"])
    registered = world.register(agent, token)
    world.register(other, other_token)
    other_claim = world.tasks.current(INPUTS["other_task"])
    world.command(agent, "op-fixture-command")
    world.clock[0] += 40
    status, ack = world.put(
        agent.execution_id,
        token,
        world.beat(
            agent,
            INPUTS["accepted_sequence"],
            archive_watermark=INPUTS["archive_watermark"],
            resources=INPUTS["resources"],
            lease_deadline_ms=INPUTS["forged_lease_deadline_ms"],
        ),
    )
    assert status == 200
    assert ack["lease_deadline_ms"] == world.clock[0] + INPUTS["lease_ms"]
    assert world.tasks.current(INPUTS["task"]).lease_deadline_ms == ack["lease_deadline_ms"]
    assert world.tasks.current(INPUTS["other_task"]) == other_claim
    return {"registered": [registered[0], stable(registered[1])], "heartbeat": [status, stable(ack)]}


def _rejection(world):
    agent, token = world.start()
    other, other_token = world.start(INPUTS["other_seat"], INPUTS["other_task"])
    world.register(agent, token)
    world.register(other, other_token)
    world.put(other.execution_id, other_token, world.beat(other, 1))
    before = world.protected()
    forged = world.put(other.execution_id, token, world.beat(other, INPUTS["accepted_sequence"]))
    generation = world.put(agent.execution_id, token, world.beat(agent, 1, generation=2))
    epoch = world.beat(agent, 1)
    epoch["authority"]["controller_epoch"] += 1
    epoch = world.put(agent.execution_id, token, epoch)
    assert world.protected() == before
    world.start(previous=agent.execution_id)
    before = world.protected()
    stale = world.put(agent.execution_id, token, world.beat(agent, INPUTS["accepted_sequence"]))
    assert world.protected() == before
    return {
        "forged_url_subject": [forged[0], forged[1]["error_class"], forged[1]["retry"]],
        "stale_task_generation": [generation[0], generation[1]["error_class"], generation[1]["retry"]],
        "other_controller_epoch": [epoch[0], epoch[1]["error_class"], epoch[1]["retry"]],
        "stale_generation": [stale[0], stale[1]["error_class"], stale[1]["retry"]],
        "heartbeat_rejections": heartbeat_rejections(world.store, SLUG),
        "heartbeat_rejections_total": heartbeat_rejections_total(world.store, SLUG),
    }


def _recovery(world):
    agent, token = world.start()
    world.register(agent, token)
    accepted = world.put(agent.execution_id, token, world.beat(agent, INPUTS["accepted_sequence"]))
    record = world.store.redis.hget(world.store.key(SLUG, "heartbeats"), agent.execution_id)
    claim = world.tasks.current(INPUTS["task"])
    world.clock[0] += 50
    restarted = ExecutionsAPI(world.grants, world.tasks, INPUTS["lease_ms"])
    replayed = restarted.route(
        "PUT",
        f"/v2/executions/{agent.execution_id}/heartbeat",
        f"Bearer {token}",
        world.beat(agent, INPUTS["accepted_sequence"]),
    )
    older = world.put(agent.execution_id, token, world.beat(agent, INPUTS["out_of_order_sequence"]))
    assert replayed == accepted
    assert world.tasks.current(INPUTS["task"]) == claim
    assert world.store.redis.hget(world.store.key(SLUG, "heartbeats"), agent.execution_id) == record
    late, late_token = world.start(INPUTS["other_seat"], INPUTS["other_task"])
    disabled = world.grants.disable(SLUG)
    try:
        world.start(f"eng-3@{SLUG}", "late")
        issued = "issued"
    except GrantRefused as error:
        issued = error.error_class
    refused = world.register(late, late_token)
    drained = world.put(agent.execution_id, token, world.beat(agent, INPUTS["accepted_sequence"] + 1))
    return {
        "replayed": [replayed[0], stable(replayed[1])],
        "out_of_order": [older[0], older[1]["error_class"]],
        "rollback": {
            "revoked_unregistered_grants": len(disabled),
            "issue_while_disabled": issued,
            "new_registration": [refused[0], refused[1]["error_class"]],
            "draining_heartbeat": [drained[0], stable(drained[1])],
        },
        "heartbeat_rejections": heartbeat_rejections(world.store, SLUG),
        "heartbeat_rejections_total": heartbeat_rejections_total(world.store, SLUG),
    }


def run_case(case):
    with pytest.MonkeyPatch.context() as patch:
        world = World(patch)
        observed = {"a": _positive, "b": _rejection, "c": _recovery}[case](world)
        return {
            "case": f"T-SV2-LDG-02-{case.upper()}",
            "state": "passed",
            "evidence_class": "mocked Redis and server clock; signed fixture grants; no external runtime",
            "input_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
            "observed": observed,
        }
