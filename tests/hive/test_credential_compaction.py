from typing import Any

import pytest

from scripts.hive import auth, hive_state

pytestmark = pytest.mark.feature("hive-credential-compaction")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    import fakeredis

    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "redis")
    monkeypatch.setenv("AGENTIHOOKS_LEGACY_CREDENTIALS", "1")
    monkeypatch.setattr(auth, "_LEDGER_OF", {})
    redis = fakeredis.FakeRedis(decode_responses=True)
    store = auth._store(redis)
    hive_state.amend(store, "member.m1", lambda _: {"name": "hive", "ledger": "d", "agents": {}})
    hive_state.amend(store, "ledger.d", lambda _: "m1")
    hive_state.amend(store, "holder.m1", lambda _: "d")
    hive_state.indexed(redis, "m1", True)
    return redis


def test_compaction_preserves_legacy_tokens_and_removes_member_maps(client: Any) -> None:
    from scripts.hive import agent_credentials

    store = auth._store(client)
    token = "hive.m1.legacy"
    held = auth._digest(token)
    hive_state.amend(
        store, "member.m1", lambda _: {"name": "hive", "ledger": "d", "agents": {held: ["alpha", "engineer@1"]}}
    )
    agent_credentials.compact(client, ["m1"], readers_compatible=True)

    assert auth.member(client, "m1") == {"name": "hive", "ledger": "d"}
    assert auth.agent_member(client, token, "alpha", "engineer@1") == "m1"
    assert auth.agent_member(client, token, "other", "engineer@1") is None
    assert hive_state.read(store, f"agent.m1.{held}") == {"ledger": "d", "scope": ["alpha", "engineer@1"]}
    assert agent_credentials.compact(client, ["m1"], readers_compatible=True) == {"m1": 31}
