import json
from unittest.mock import patch

import pytest

from scripts.hive import auth
from scripts.swarm.store import AgentRecord, RedisStore
from scripts.swarm_ledger import ledger_authority as authority
from tests.swarm_ledger.test_ledger_authority import MASTER, SLUG, WORKER, send
from tests.swarm_ledger.test_ledger_authority import live as _authority_live

live = _authority_live
pytestmark = pytest.mark.unit


@pytest.fixture
def hive():
    import fakeredis

    redis = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    grant = auth.exchange(redis, auth.invite(redis, "member"), "redis://localhost:6379/0")
    store = RedisStore(redis)
    store.names.adopt(SLUG, "323133", "", "")
    for name in (WORKER, MASTER):
        redis.hset(store.names.key("name", name), mapping={"swarm": SLUG, "code": "323133"})
    with patch("scripts.swarm.store.redis_client", return_value=redis):
        yield RedisStore(redis), grant


def mint(live, grant, name, slug=SLUG):
    status, body, _ = send(
        live,
        "POST",
        f"/api/v1/ledgers/{slug}/agent-token",
        b"{}",
        **{"X-Hive-Credential": grant["ledger_credential"], "X-Ledger-Agent": name},
    )
    return status, json.loads(body)


@pytest.mark.parametrize("name", [WORKER, MASTER, "unplaced-agent"])
def test_a_member_cannot_mint_for_an_unplaced_agent(live, hive, name):
    _, grant = hive
    status, reply = mint(live, grant, name)
    assert status == 403
    assert reply["error"]["code"] == "forbidden"


@pytest.mark.parametrize("owner", ["", "other-member"])
@pytest.mark.parametrize("name,lane,seat", [(WORKER, "eng", f"eng-1@{SLUG}"), (MASTER, "master", f"master@{SLUG}")])
def test_a_member_cannot_mint_for_another_hives_agent(live, hive, owner, name, lane, seat):
    store, grant = hive
    store.start_execution(SLUG, AgentRecord(name, lane, "proof", seat=seat, hive=owner))
    status, _ = mint(live, grant, name)
    assert status == 403


@pytest.mark.parametrize("name,lane", [(WORKER, "eng"), (MASTER, "master")])
def test_a_member_can_mint_for_its_placed_worker_or_master(live, hive, name, lane):
    store, grant = hive
    store.start_execution(
        SLUG,
        AgentRecord(
            name, lane, "proof", seat=f"master@{SLUG}" if lane == "master" else f"eng-1@{SLUG}", hive=grant["id"]
        ),
    )
    status, reply = mint(live, grant, name)
    assert status == 200
    status, _, _ = send(
        live,
        "GET",
        f"/api/v1/ledgers/{SLUG}",
        **{
            "X-Ledger-Token": reply["data"]["token"],
            "X-Ledger-Agent": name,
        },
    )
    assert status == 200


def test_revoking_a_member_kills_every_issued_agent_token(live, hive):
    store, grant = hive
    store.start_execution(SLUG, AgentRecord(WORKER, "eng", "proof", seat=f"eng-1@{SLUG}", hive=grant["id"]))
    tokens = [mint(live, grant, WORKER)[1]["data"]["token"] for _ in range(2)]
    auth.revoke(store.redis, grant["id"])
    for token in tokens:
        status, _, _ = send(
            live,
            "GET",
            f"/api/v1/ledgers/{SLUG}",
            **{
                "X-Ledger-Token": token,
                "X-Ledger-Agent": WORKER,
            },
        )
        assert status == 403
    assert mint(live, grant, WORKER)[0] == 403


def test_a_member_token_is_bound_to_its_ledger_and_agent(live, hive):
    store, grant = hive
    store.start_execution(SLUG, AgentRecord(WORKER, "eng", "proof", seat=f"eng-1@{SLUG}", hive=grant["id"]))
    token = mint(live, grant, WORKER)[1]["data"]["token"]
    assert authority.principal(live["admin"], SLUG, token, WORKER) == WORKER
    assert authority.principal(live["admin"], "another-ledger", token, WORKER) is None
    assert authority.principal(live["admin"], SLUG, token, MASTER) is None
    assert authority.principal(live["admin"], SLUG, token + "forged", WORKER) is None


def test_revoke_keeps_another_members_tokens_working(live, hive):
    store, grant = hive
    other = auth.exchange(store.redis, auth.invite(store.redis, "other"), "redis://localhost:6379/0")
    store.start_execution(SLUG, AgentRecord(WORKER, "eng", "proof", seat=f"eng-1@{SLUG}", hive=other["id"]))
    token = mint(live, other, WORKER)[1]["data"]["token"]
    auth.revoke(store.redis, grant["id"])
    assert authority.principal(live["admin"], SLUG, token, WORKER) == WORKER


def test_a_token_stops_working_when_its_agent_moves_to_another_hive(live, hive):
    store, grant = hive
    store.start_execution(SLUG, AgentRecord(WORKER, "eng", "proof", seat=f"eng-1@{SLUG}", hive=grant["id"]))
    token = mint(live, grant, WORKER)[1]["data"]["token"]
    from dataclasses import replace

    store.put_agent(SLUG, replace(store.agents(SLUG)[0], hive="another-member"))
    assert authority.principal(live["admin"], SLUG, token, WORKER) is None


def test_a_revoked_member_cannot_be_recreated_by_token_issuance(hive):
    store, grant = hive
    auth.revoke(store.redis, grant["id"])
    with pytest.raises(auth.HiveError, match="Hive membership was revoked"):
        auth.issue_agent(store.redis, grant["id"], SLUG, WORKER)


def test_revoke_conflicting_with_token_recording_cannot_recreate_membership(hive):
    store, grant = hive
    transact = store.redis.transaction
    revoked = []

    def concurrent_record(callback, key):
        def record(pipe):
            callback(pipe)
            if not revoked:
                revoked.append(True)
                auth.revoke(store.redis, grant["id"])

        return transact(record, key)

    with patch.object(store.redis, "transaction", side_effect=concurrent_record):
        with pytest.raises(auth.HiveError, match="Hive membership was revoked"):
            auth.issue_agent(store.redis, grant["id"], SLUG, WORKER)
    assert auth.ledger_member(store.redis, grant["ledger_credential"]) is None


def test_revoke_during_token_minting_is_refused(live, hive):
    store, grant = hive
    store.start_execution(SLUG, AgentRecord(WORKER, "eng", "proof", seat=f"eng-1@{SLUG}", hive=grant["id"]))
    issue = auth.issue_agent

    def revoked(redis, member, slug, name):
        auth.revoke(redis, member)
        return issue(redis, member, slug, name)

    with patch.object(auth, "issue_agent", side_effect=revoked):
        assert mint(live, grant, WORKER)[0] == 403


@pytest.mark.parametrize("mode", ["compose", "distributed"])
@pytest.mark.parametrize("name", [WORKER, MASTER])
def test_previously_issued_page_derived_tokens_are_refused_remotely(live, hive, monkeypatch, mode, name):
    store, grant = hive
    token = authority.agent_token(live["admin"], SLUG, name)
    auth.revoke(store.redis, grant["id"])
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", mode)
    assert authority.principal(live["admin"], SLUG, token, name) is None


def test_local_page_derived_agent_tokens_still_work(live, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "local")
    token = authority.agent_token(live["admin"], SLUG, WORKER)
    assert authority.principal(live["admin"], SLUG, token, WORKER) == WORKER
