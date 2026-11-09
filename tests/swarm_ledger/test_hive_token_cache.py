"""The ledger authority checks a hive agent token against the credential store once, then from a bounded cache."""

from collections import OrderedDict
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.hive import auth
from scripts.swarm_ledger import ledger_authority as authority
from scripts.swarm_ledger import ports

pytestmark = pytest.mark.unit

SLUG = "alpha"
WORKER = "engineer@1"
REQUESTS = 20


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [1000.0]
    monkeypatch.setattr(authority, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


@pytest.fixture
def placed(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    hosted: set[str] = set()

    def on_hive(member: str, slug: str, name: str) -> bool:
        return member in hosted

    monkeypatch.setattr(authority, "hive_agent", on_hive)
    return hosted


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, placed: set[str]) -> Any:
    import fakeredis

    monkeypatch.setattr(auth, "_LEDGER_OF", {})
    monkeypatch.setattr(authority, "_HIVE_CHECKS", OrderedDict())
    redis = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    monkeypatch.setattr(ports.swarm().store, "redis_client", lambda: redis)
    return redis


@pytest.fixture
def member(client: Any, placed: set[str]) -> str:
    grant = auth.mint(client, "member", "redis://localhost:6379/0")
    placed.add(grant["id"])
    return grant["id"]


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
def test_repeated_requests_with_one_hive_token_make_one_credential_store_read(
    client: Any, member: str, lookups: list[str], clock: list[float]
) -> None:
    token = auth.issue_agent(client, member, SLUG, WORKER)

    answers = {authority.hive_principal(SLUG, token, WORKER) for _ in range(REQUESTS)}

    assert answers == {WORKER}
    assert lookups == [token]


@pytest.mark.feature("hive-agent-token-cache")
def test_a_revoked_hive_token_is_refused_once_its_cached_check_expires(
    client: Any, member: str, lookups: list[str], clock: list[float]
) -> None:
    token = auth.issue_agent(client, member, SLUG, WORKER)
    assert authority.hive_principal(SLUG, token, WORKER) == WORKER

    auth.revoke(client, member)
    clock[0] += authority.HIVE_CHECK_TTL_S / 2
    assert authority.hive_principal(SLUG, token, WORKER) == WORKER
    assert len(lookups) == 1

    clock[0] += authority.HIVE_CHECK_TTL_S
    assert authority.hive_principal(SLUG, token, WORKER) is None
    assert authority.hive_principal(SLUG, token, WORKER) is None
    assert len(lookups) == 3


@pytest.mark.feature("hive-agent-token-cache")
def test_a_cached_hive_token_is_refused_once_its_agent_leaves_the_hive(
    client: Any, member: str, lookups: list[str], placed: set[str], clock: list[float]
) -> None:
    token = auth.issue_agent(client, member, SLUG, WORKER)
    assert authority.hive_principal(SLUG, token, WORKER) == WORKER

    placed.discard(member)

    assert authority.hive_principal(SLUG, token, WORKER) is None
    assert lookups == [token]


@pytest.mark.feature("hive-agent-token-cache")
def test_a_cached_hive_token_check_holds_only_its_own_ledger_and_agent(
    client: Any, member: str, lookups: list[str], clock: list[float]
) -> None:
    token = auth.issue_agent(client, member, SLUG, WORKER)
    assert authority.hive_principal(SLUG, token, WORKER) == WORKER

    assert authority.hive_principal("beta", token, WORKER) is None
    assert authority.hive_principal(SLUG, token, "engineer@2") is None
    assert len(lookups) == 3


@pytest.mark.feature("hive-agent-token-cache")
def test_the_hive_token_cache_keeps_at_most_its_bound(
    client: Any, member: str, lookups: list[str], clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(authority, "HIVE_CHECK_MAX", 2)
    tokens = [auth.issue_agent(client, member, SLUG, f"engineer@{n}") for n in range(3)]
    for n, token in enumerate(tokens):
        assert authority.hive_principal(SLUG, token, f"engineer@{n}") == f"engineer@{n}"

    assert authority.hive_principal(SLUG, tokens[0], "engineer@0") == "engineer@0"
    assert len(lookups) == 4
