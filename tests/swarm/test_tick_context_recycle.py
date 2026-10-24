from dataclasses import replace

import pytest

from hooks.context import context_recycle
from scripts.swarm import tick as tick_module
from scripts.swarm.store import RedisStore, SwarmConfig
from tests.swarm.test_tick import FakeLedger, FakeRuntime


@pytest.fixture
def store():
    import fakeredis

    result = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    result.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1))
    return result


@pytest.fixture(autouse=True)
def isolated_recycling(tmp_path, monkeypatch):
    monkeypatch.setattr(context_recycle, "AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr(context_recycle, "COMPACT_LIMIT", 600)
    monkeypatch.setattr(context_recycle, "HANDOFF_MARGIN", 50)
    monkeypatch.setattr(context_recycle, "used_tokens", lambda session_id: None)
    monkeypatch.setattr(context_recycle, "codex_context", lambda session_id: None)


@pytest.mark.parametrize("pane", ["w1:p1", "w1:m1"])
def test_tick_bound_session_gets_handoff_without_identity_in_any_hook(store, monkeypatch, pane):
    runtime = FakeRuntime()
    runtime.conversation_ids = {pane: "bound", "unowned:pane": "unbound", f"{pane}/child": "nested"}
    tick_module.tick("sw", store, FakeLedger([{"id": "t1"}]), runtime, 1000)
    assert "bound" in {agent.conversation_id for agent in store.agents("sw")}
    monkeypatch.setattr(context_recycle, "used_tokens", lambda session_id: 600_000)
    assert "HANDOFF PREPARATION" in context_recycle.directive("bound", {})
    assert context_recycle.gate("Bash", {"command": "git commit"}, "bound", {}) is None
    monkeypatch.setattr(context_recycle, "used_tokens", lambda session_id: 650_000)
    assert "agentihooks swarm sw handoff" in context_recycle.directive("bound", {})
    assert "BLOCKED" in context_recycle.gate("Bash", {"command": "git commit"}, "bound", {})
    for session in ("unbound", "nested", ""):
        assert context_recycle.directive(session, {}) is None
        assert context_recycle.gate("Bash", {"command": "git commit"}, session, {}) is None


def test_tick_pins_an_already_known_conversation_without_a_prior_hook(store, monkeypatch):
    runtime = FakeRuntime()
    ledger = FakeLedger([{"id": "t1"}])
    tick_module.tick("sw", store, ledger, runtime, 1000)
    agent = next(agent for agent in store.agents("sw") if agent.lane == "eng")
    store.put_agent("sw", replace(agent, conversation_id="known"))
    runtime.conversation_ids = {agent.pane_id: "known"}
    tick_module.tick("sw", store, ledger, runtime, 2000)
    monkeypatch.setattr(context_recycle, "used_tokens", lambda session_id: 650_000)
    assert "agentihooks swarm sw handoff" in context_recycle.directive("known", {})


@pytest.mark.parametrize("compact_limit", [0, 700])
def test_tick_pin_retains_the_swarm_limit_and_an_existing_session_pin(store, monkeypatch, compact_limit):
    runtime = FakeRuntime()
    ledger = FakeLedger([{"id": "t1"}])
    store.update("sw", compact_limit=compact_limit)
    runtime.conversation_ids = {"w1:p1": "bound"}
    tick_module.tick("sw", store, ledger, runtime, 1000)
    monkeypatch.setattr(context_recycle, "used_tokens", lambda session_id: 650_000)
    expected = None if compact_limit else 650_000
    assert context_recycle.unmarked_overrun("bound", 900) == expected
    store.update("sw", compact_limit=900)
    tick_module.tick("sw", store, ledger, runtime, 2000)
    assert context_recycle.unmarked_overrun("bound", 900) == expected
