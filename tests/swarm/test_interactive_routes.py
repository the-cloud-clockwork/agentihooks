import json
from dataclasses import replace
from types import SimpleNamespace

import fakeredis
import pytest

from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import AgentRecord, RedisStore
from scripts.swarm.tick import Placed, placed_record
from tests.swarm.test_runtime import _passed, _resuming, validated


@pytest.mark.parametrize("kind", ["subscription", "interactive", "api"])
def test_route_kind_persists_and_legacy_records_default_to_subscription(kind):
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    agent = AgentRecord("agent", "master", "master", route_kind=kind)
    store.put_agent("proof", agent)
    assert store.agents("proof")[0].route_kind == kind
    raw = json.loads(store.redis.hget(store.key("proof", "agents"), "agent"))
    del raw["route_kind"]
    store.redis.hset(store.key("proof", "agents"), "agent", json.dumps(raw))
    assert store.agents("proof")[0].route_kind == "subscription"


def test_placement_copies_interactive_route_kind_to_the_persisted_record():
    agent = placed_record(AgentRecord("agent", "master", "master"), Placed("pane", "claude", route_kind="interactive"))
    assert agent.route_kind == "interactive"


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_resume_preserves_interactive_route_when_the_account_also_has_a_token(tmp_path, harness):
    runtime, config, agent, seen = _resuming(tmp_path, "c0ffee", harness)
    agent = replace(agent, route_kind="interactive")
    placed = runtime.resume(config, agent, "resume")
    passed = _passed(seen["runs"][0])
    assert passed[passed.index("--route") + 1] == "interactive"
    assert placed.route_kind == "interactive"


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_spawn_preserves_explicit_interactive_route_kind(tmp_path, harness):
    seen = []

    def run(argv, **kwargs):
        seen.append(argv)
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: (harness, "requested"))
    config = SimpleNamespace(
        slug="proof",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        lanes={"master": {"agent": harness}},
        autonomy="delegate",
    )
    placed = runtime.spawn(config, "master", "agent", {"id": "master", "title": "proof", "route_kind": "interactive"})
    passed = _passed(seen[0])
    assert passed[passed.index("--route") + 1] == "interactive"
    assert placed.route_kind == "interactive"
