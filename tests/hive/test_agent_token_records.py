"""Hive agent token issuance keeps one bounded record per agent in the hive credential store."""

from typing import Any

import pytest

from scripts.hive import auth, hive_state

pytestmark = pytest.mark.unit

COMMANDS = 12


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    import fakeredis

    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "redis")
    monkeypatch.setattr(auth, "_LEDGER_OF", {})
    redis = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    store = auth._store(redis)
    hive_state.amend(store, "member.m1", lambda _: {"name": "n", "ledger": "d", "agents": {}})
    hive_state.amend(store, "ledger.d", lambda _: "m1")
    hive_state.amend(store, "holder.m1", lambda _: "d")
    return redis


def _current(client: Any) -> str:
    """The Redis hash holding the hive credential store's current records."""
    store: Any = auth._store(client)
    return store._current


def _agent_fields(client: Any) -> list[str]:
    """The credential store fields that record agent tokens of member m1."""
    return [field for field in client.hkeys(_current(client)) if field.startswith("agent.m1.")]


def _agent_keys(client: Any) -> list[str]:
    """The Redis keys whose name carries an agent token record of member m1."""
    return [key for key in client.keys("*") if "agent.m1." in key]


def test_repeated_commands_from_one_agent_leave_one_agent_record(client: Any) -> None:
    tokens = [auth.issue_agent(client, "m1", "alpha", "engineer@1") for _ in range(COMMANDS)]

    assert len(set(tokens)) == COMMANDS
    assert len(_agent_fields(client)) == 1
    assert len(_agent_keys(client)) == 1
    assert auth.agent_member(client, tokens[-1], "alpha", "engineer@1") == "m1"


def test_an_agent_record_keeps_only_its_newest_tokens(client: Any) -> None:
    tokens = [auth.issue_agent(client, "m1", "alpha", "engineer@1") for _ in range(COMMANDS)]
    kept = tokens[-auth.AGENT_TOKENS_KEPT :]

    for token in tokens:
        expected = "m1" if token in kept else None
        assert auth.agent_member(client, token, "alpha", "engineer@1") == expected
    for token in kept:
        assert auth.agent_member(client, token, "alpha", "engineer@2") is None
        assert auth.agent_member(client, token, "beta", "engineer@1") is None


def test_each_agent_keeps_its_own_record(client: Any) -> None:
    first = [auth.issue_agent(client, "m1", "alpha", "engineer@1") for _ in range(3)]
    second = [auth.issue_agent(client, "m1", "alpha", "engineer@2") for _ in range(3)]

    assert len(_agent_fields(client)) == 2
    assert all(auth.agent_member(client, token, "alpha", "engineer@1") == "m1" for token in first)
    assert all(auth.agent_member(client, token, "alpha", "engineer@2") == "m1" for token in second)
    assert all(auth.agent_member(client, token, "alpha", "engineer@1") is None for token in second)
