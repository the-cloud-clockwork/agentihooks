"""The ledger authority checks a hive agent token against the credential store once, then from a bounded cache."""

from typing import Any

import pytest

from scripts.hive import auth, hive_state
from scripts.swarm_ledger import ledger_authority as authority
from scripts.swarm_ledger import ports

pytestmark = pytest.mark.unit

SLUG = "alpha"
WORKER = "engineer@1"
REQUESTS = 20


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    import fakeredis

    monkeypatch.setenv("AGENTIHOOKS_STATE_BACKEND", "redis")
    monkeypatch.setattr(auth, "_LEDGER_OF", {})
    authority.forget_hive_checks()
    redis = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    store = auth._store(redis)
    hive_state.amend(store, "member.m1", lambda _: {"name": "n", "ledger": "d", "agents": {}})
    hive_state.amend(store, "ledger.d", lambda _: "m1")
    hive_state.amend(store, "holder.m1", lambda _: "d")
    monkeypatch.setattr(ports.swarm().store, "redis_client", lambda: redis)
    monkeypatch.setattr(authority, "hive_agent", lambda member, slug, name: member == "m1")
    yield redis
    authority.forget_hive_checks()


@pytest.fixture
def lookups(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    check = ports.hive().auth.agent_member

    def counted(redis: object, token: str, slug: str, name: str) -> str | None:
        seen.append(token)
        return check(redis, token, slug, name)

    monkeypatch.setattr(ports.hive().auth, "agent_member", counted)
    return seen


@pytest.mark.feature("hive-agent-token-cache")
def test_repeated_requests_with_one_hive_token_make_one_store_read(client: Any, lookups: list[str]) -> None:
    token = auth.issue_agent(client, "m1", SLUG, WORKER)

    answers = {authority.hive_principal(SLUG, token, WORKER) for _ in range(REQUESTS)}

    assert answers == {WORKER}
    assert lookups == [token]


@pytest.mark.feature("hive-agent-token-cache")
def test_a_revoked_hive_token_is_refused_once_its_check_ages_out(
    client: Any, lookups: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    token = auth.issue_agent(client, "m1", SLUG, WORKER)
    assert authority.hive_principal(SLUG, token, WORKER) == WORKER

    hive_state.amend(auth._store(client), "ledger.d", lambda _: None)
    monkeypatch.setattr(authority, "HIVE_CHECK_TTL_S", 0.0)

    assert authority.hive_principal(SLUG, token, WORKER) is None
    assert authority.hive_principal(SLUG, token, WORKER) is None
    assert len(lookups) == 3


@pytest.mark.feature("hive-agent-token-cache")
def test_a_cached_hive_token_check_holds_only_its_own_ledger_and_agent(client: Any, lookups: list[str]) -> None:
    token = auth.issue_agent(client, "m1", SLUG, WORKER)
    assert authority.hive_principal(SLUG, token, WORKER) == WORKER

    assert authority.hive_principal("beta", token, WORKER) is None
    assert authority.hive_principal(SLUG, token, "engineer@2") is None
    assert len(lookups) == 3


@pytest.mark.feature("hive-agent-token-cache")
def test_the_hive_token_cache_keeps_at_most_its_bound(
    client: Any, lookups: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(authority, "HIVE_CHECK_MAX", 2)
    tokens = [auth.issue_agent(client, "m1", SLUG, f"engineer@{n}") for n in range(3)]
    for n, token in enumerate(tokens):
        assert authority.hive_principal(SLUG, token, f"engineer@{n}") == f"engineer@{n}"

    assert authority.hive_principal(SLUG, tokens[0], "engineer@0") == "engineer@0"
    assert len(lookups) == 4
