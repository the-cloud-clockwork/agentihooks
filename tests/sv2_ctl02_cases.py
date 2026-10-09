import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import fakeredis
import pytest

from scripts.swarm import lease
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm_v2.auth_context import LaunchAuthority, LaunchKey
from scripts.swarm_v2.authority import TaskAuthority
from scripts.swarm_v2.controller import Controller

FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/task-authority.json"


def build(monkeypatch):
    inputs = json.loads(FIXTURE.read_text(encoding="utf-8"))
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(inputs["swarm"], "agentihooks", 2, 0))
    clock = [inputs["clock_ms"]]
    monkeypatch.setattr(lease, "now_ms", lambda store: clock[0])
    controller = Controller(store, inputs["swarm"], [], lambda: True)
    assert controller.acquire()
    grants = LaunchAuthority(
        store, LaunchKey("fixture-key", b"x" * 32), "controller", "claims", lambda: clock[0] / 1000
    )
    identities = {}

    def authorize(token):
        agent = identities[token]
        return grants.register(
            inputs["swarm"], token, {"execution_id": agent.execution_id, "generation": agent.generation}
        )

    authority = TaskAuthority(store, controller, authorize)

    def start(seat=inputs["old_seat"], previous="", task=inputs["task"]):
        agent = controller.admit(AgentRecord(store.next_name(inputs["swarm"], "eng"), "eng", task, seat=seat), previous)
        token = grants.issue(
            inputs["swarm"], agent.execution_id, project_ids=[inputs["project"]], brain_id="swarm", account="fixture"
        )
        identities[token] = agent
        return agent, token

    return store, authority, controller, clock, start


def run_case(case):
    with pytest.MonkeyPatch.context() as patch:
        inputs = json.loads(FIXTURE.read_text(encoding="utf-8"))
        store, authority, controller, clock, start = build(patch)
        old, token = start()
        first = authority.admit(token, inputs["lease_ms"])
        clock[0] = inputs["partition_until_ms"]
        new, successor_token = start(inputs["new_seat"])
        second = authority.admit(successor_token, inputs["lease_ms"])
        assert second.generation == 2
        before = authority.journal(inputs["task"])
        rejected = ""
        if case in ("a", "b"):
            try:
                if case == "a":
                    authority.renew(token, first.generation, inputs["lease_ms"])
                else:
                    authority.release(token, first.generation)
            except SwarmError as error:
                rejected = str(error)
            assert rejected == "stale_generation"
            assert authority.current(inputs["task"]) == second
            assert authority.journal(inputs["task"]) == before
            assert store.claimant(inputs["swarm"], inputs["task"]) == new.name
        else:
            assert case == "c"
            second = authority.complete(successor_token, 2, {"outcome": "done"})
            journal = authority.journal(inputs["task"])
            store.redis.delete(store.key(inputs["swarm"], "task-authority", inputs["task"]))
            assert authority.replay(inputs["task"]) == second
            assert authority.replay(inputs["task"]) == second
            assert authority.journal(inputs["task"]) == journal
            assert store.claimant(inputs["swarm"], inputs["task"]) is None
            controller.admission_enabled = False
            with pytest.raises(SwarmError) as error:
                authority.admit(token, inputs["lease_ms"])
            assert str(error.value) == "controller admission is disabled until reconciliation completes"
            assert authority.current(inputs["task"]) == second
            assert authority.journal(inputs["task"]) == journal
            assert store.claim(inputs["swarm"], "local", "local-worker", 100)
        return {
            "case": f"T-SV2-CTL-02-{case.upper()}",
            "state": "passed",
            "evidence_class": "mocked Redis and server clock; signed fixture grants; no external runtime",
            "input_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
            "observed": asdict(authority.current(inputs["task"])),
            "rejection": rejected,
            "stale_generation_rejections_total": authority.stale_generation_rejections_total(),
            "journal_events": [row["event"] for row in authority.journal(inputs["task"])],
            "rollback": "new admission disabled; generations and journal retained; local compatibility works"
            if case == "c"
            else "not exercised",
        }
