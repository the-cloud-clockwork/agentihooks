from unittest.mock import patch

import pytest

from scripts.hive import auth

SLUG = "alpha"
WORKER = "engineer@323133-0256"


@pytest.fixture
def client():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


def test_issuing_a_token_never_reads_a_whole_member_map(client):
    grant = auth.mint(client, "laptop", "redis://hive.example:6380/2")

    with (
        patch.object(auth, "member", side_effect=AssertionError("read the member map")),
        patch.object(auth, "_legacy_member", side_effect=AssertionError("read the legacy member map")),
    ):
        token = auth.issue_agent(client, grant["id"], SLUG, WORKER)
        assert auth.agent_member(client, token, SLUG, WORKER) == grant["id"]


def test_a_minted_member_records_its_credential_digest_on_its_own(client):
    grant = auth.mint(client, "laptop", "redis://hive.example:6380/2")

    held = auth.hive_state.read(auth._store(client), f"holder.{grant['id']}")

    assert held == auth._digest(grant["ledger_credential"])


def test_a_member_minted_before_the_holder_record_still_issues_tokens(client):
    grant = auth.mint(client, "laptop", "redis://hive.example:6380/2")
    auth.hive_state.amend(auth._store(client), f"holder.{grant['id']}", lambda _: None)

    token = auth.issue_agent(client, grant["id"], SLUG, WORKER)

    assert auth.agent_member(client, token, SLUG, WORKER) == grant["id"]


def test_a_member_without_a_holder_record_is_refused_without_a_map_read_once_legacy_is_off(client, monkeypatch):
    grant = auth.mint(client, "laptop", "redis://hive.example:6380/2")
    auth.hive_state.amend(auth._store(client), f"holder.{grant['id']}", lambda _: None)
    monkeypatch.setenv("AGENTIHOOKS_AUTH_LEGACY", "off")

    with patch.object(auth, "member", side_effect=AssertionError("read the member map")):
        with pytest.raises(auth.HiveError, match="^Hive membership was revoked$"):
            auth.issue_agent(client, grant["id"], SLUG, WORKER)


def test_revoking_a_member_drops_its_holder_record_and_refuses_issue(client):
    grant = auth.mint(client, "laptop", "redis://hive.example:6380/2")

    auth.revoke(client, grant["id"])

    assert auth.hive_state.read(auth._store(client), f"holder.{grant['id']}") is None
    with pytest.raises(auth.HiveError, match="^Hive membership was revoked$"):
        auth.issue_agent(client, grant["id"], SLUG, WORKER)


def test_an_unknown_member_is_refused_issue(client):
    with pytest.raises(auth.HiveError, match="^Hive membership was revoked$"):
        auth.issue_agent(client, "0123456789abcdef", SLUG, WORKER)


def test_a_failed_mint_forgets_the_holder_record(client):
    with patch.object(auth, "_set_user", side_effect=auth.redis_acl.Refused("refused")):
        with pytest.raises(auth.HiveError):
            auth.mint(client, "laptop", "redis://hive.example:6380/2")

    assert not [key for key in client.keys("*") if "holder" in key]


def test_a_token_missing_its_record_reads_the_member_map_only_with_legacy_credentials_on(client, monkeypatch):
    grant = auth.mint(client, "laptop", "redis://hive.example:6380/2")
    unknown = f"hive.{grant['id']}.unknown"
    monkeypatch.setenv("AGENTIHOOKS_AUTH_LEGACY", "off")

    with patch.object(auth, "member", side_effect=AssertionError("read the member map")):
        assert auth.agent_member(client, unknown, SLUG, WORKER) is None
    monkeypatch.setenv("AGENTIHOOKS_AUTH_LEGACY", "on")
    with patch.object(auth, "member", return_value={"agents": {auth._digest(unknown): [SLUG, WORKER]}}) as read:
        assert auth.agent_member(client, unknown, SLUG, WORKER) == grant["id"]
    read.assert_called_once_with(client, grant["id"])
