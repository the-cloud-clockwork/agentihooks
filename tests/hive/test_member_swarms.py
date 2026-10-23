import hashlib
import urllib.parse

import pytest

from scripts import openbao_kv
from scripts.events import nkey
from scripts.hive import auth, cli, invite, member_swarms, nats_grant, server
from scripts.swarm.store import PREFIX as SWARMS
from tests.codex_fakes import BAO_TOKEN, BAO_URL, http_error
from tests.hive.test_invite import RECORD, URL, Bao

pytestmark = pytest.mark.unit

SIGNER = "agentihooks/hub/nats-signer"
PUBLIC = "rediss://hive.example:6380/2"


@pytest.fixture
def admin():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


def _bao(fake):
    return openbao_kv.OpenBao(BAO_URL, BAO_TOKEN, fake)


def _signer(listed):
    operator, account = nkey.create(nkey.OPERATOR), nkey.create(nkey.ACCOUNT)
    return nats_grant.Signer(operator, account, "system", "nats://127.0.0.1:1", operator.public, listed)


def _password(admin, member_id):
    return admin.acl_getuser(f"hive-{member_id}")["passwords"]


def test_the_signer_path_is_the_hub_openbao_record_or_nothing():
    assert member_swarms.signer_path({}) is None
    assert member_swarms.signer_path({nats_grant.SIGNER_ENV: SIGNER}) == SIGNER


@pytest.mark.parametrize("path", ["file:/run/signer", "k8s/other"])
def test_a_signer_outside_the_hub_openbao_path_has_no_member_swarms_record(path):
    assert member_swarms.signer_path({nats_grant.SIGNER_ENV: path}) is None


def test_named_swarms_win_and_no_name_means_every_swarm(admin):
    admin.sadd(f"{SWARMS}:index", "rig", "alpha")
    assert member_swarms.swarms(admin, ["only"]) == ("only",)
    assert member_swarms.swarms(admin, []) == ("alpha", "rig")


def test_no_swarm_at_all_is_refused(admin):
    with pytest.raises(auth.HiveError) as error:
        member_swarms.swarms(admin, [])
    assert str(error.value) == "no swarm to list: pass --swarm or create a swarm first"


def test_assign_lists_only_unlisted_members_and_keeps_the_rest_of_the_record():
    fake = Bao({SIGNER: {"account_seed": "kept", "member_swarms": {"m1": ["old"]}}})
    added = member_swarms.assign(_bao(fake), SIGNER, ["m1", "m2", "m3"], ("rig", "second"))
    assert added == ["m2", "m3"]
    assert fake.records[SIGNER] == {
        "account_seed": "kept",
        "member_swarms": {"m1": ["old"], "m2": ["rig", "second"], "m3": ["rig", "second"]},
    }


def test_assign_creates_the_entry_on_a_record_without_one_only_when_asked():
    fake = Bao({SIGNER: {"account_seed": "kept"}})
    assert member_swarms.assign(_bao(fake), SIGNER, ["m1"], ("rig",)) == []
    assert fake.written == []
    assert member_swarms.assign(_bao(fake), SIGNER, ["m1"], ("rig",), create=True) == ["m1"]
    assert fake.records[SIGNER] == {"account_seed": "kept", "member_swarms": {"m1": ["rig"]}}


def test_assign_refuses_an_invalid_swarm_and_writes_nothing():
    fake = Bao({SIGNER: {"account_seed": "kept", "member_swarms": {}}})
    with pytest.raises(auth.HiveError) as error:
        member_swarms.assign(_bao(fake), SIGNER, ["m1"], ("bad.name",))
    assert str(error.value).startswith("member_swarms contains an invalid swarm name")
    assert fake.written == []


def test_assign_reports_an_openbao_failure_as_a_hive_error():
    fake = Bao({SIGNER: {"member_swarms": {}}}, fail_writes=True)
    with pytest.raises(auth.HiveError) as error:
        member_swarms.assign(_bao(fake), SIGNER, ["m1"], ("rig",))
    assert str(error.value) == "OpenBao HTTP 503: sealed"


def test_assign_rereads_the_record_after_a_concurrent_write():
    fake = Bao({SIGNER: {"member_swarms": {}}})
    plain = fake.__call__
    raced = []

    def racing(request, timeout=None):
        if request.get_method() != "GET" and not raced:
            raced.append(True)
            fake.records[SIGNER] = {"member_swarms": {"m1": ["other"]}}
            fake.versions[SIGNER] += 1
            raise http_error(request.full_url, 400, b'{"errors": ["check-and-set parameter did not match"]}')
        return plain(request, timeout)

    assert member_swarms.assign(openbao_kv.OpenBao(BAO_URL, BAO_TOKEN, racing), SIGNER, ["m1", "m2"], ("rig",)) == [
        "m2"
    ]
    assert fake.records[SIGNER]["member_swarms"] == {"m1": ["other"], "m2": ["rig"]}


def test_backfill_lists_every_joined_member_from_the_hive_list(admin):
    first, second = (auth.mint(admin, name, PUBLIC)["id"] for name in ("h1", "h2"))
    fake = Bao({SIGNER: {"member_swarms": {first: ["rig"]}}})
    outcome = member_swarms.backfill(admin, _bao(fake), SIGNER, ["rig"])
    assert outcome == f"OpenBao secret/{SIGNER}: wrote member_swarms for {second}"
    assert fake.records[SIGNER]["member_swarms"] == {first: ["rig"], second: ["rig"]}
    writes = list(fake.written)
    assert member_swarms.backfill(admin, _bao(fake), SIGNER, ["rig"]) == (
        f"OpenBao secret/{SIGNER}: wrote member_swarms for no member"
    )
    assert fake.written == writes


def test_backfill_with_no_member_still_turns_scoping_on():
    import fakeredis

    fake = Bao({SIGNER: {"account_seed": "kept"}})
    redis = fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)
    member_swarms.backfill(redis, _bao(fake), SIGNER, ["rig"])
    assert fake.records[SIGNER] == {"account_seed": "kept", "member_swarms": {}}


def test_listing_reports_an_openbao_failure_as_a_hive_error():
    def sealed(request, timeout=None):
        raise http_error(request.full_url, 503, b'{"errors": ["sealed"]}')

    with pytest.raises(auth.HiveError) as error:
        member_swarms.listing(openbao_kv.OpenBao(BAO_URL, BAO_TOKEN, sealed), SIGNER)
    assert str(error.value) == "OpenBao HTTP 503: sealed"


def _scoped_line(fake):
    return [line for line in fake.records[RECORD]["credential"].splitlines() if line.startswith("AGENTIHOOKS_BUS_S")]


def test_invite_lists_the_new_member_in_a_backfilled_signer_record_and_marks_the_invite_scoped(admin):
    fake = Bao({SIGNER: {"account_seed": "kept", "member_swarms": {"m0": ["old"]}}})
    request = invite.Request("lab", RECORD, url=URL, redis_url=PUBLIC, signer_path=SIGNER, swarms=("rig",))
    invite.invite(admin, _bao(fake), request, PUBLIC)
    (member,) = auth_members(admin)
    assert fake.records[SIGNER] == {"account_seed": "kept", "member_swarms": {"m0": ["old"], member: ["rig"]}}
    assert _scoped_line(fake) == ["AGENTIHOOKS_BUS_SCOPED=1"]


def test_invite_before_the_backfill_leaves_the_record_unscoped_and_the_invite_legacy(admin):
    fake = Bao({SIGNER: {"account_seed": "kept"}})
    request = invite.Request("lab", RECORD, url=URL, redis_url=PUBLIC, signer_path=SIGNER, swarms=("rig",))
    invite.invite(admin, _bao(fake), request, PUBLIC)
    assert fake.records[SIGNER] == {"account_seed": "kept"}
    assert _scoped_line(fake) == []
    assert len(auth_members(admin)) == 1


def test_invite_without_a_signer_record_leaves_member_swarms_alone(admin):
    fake = Bao({SIGNER: {"account_seed": "kept", "member_swarms": {}}})
    invite.invite(admin, _bao(fake), invite.Request("lab", RECORD, url=URL, redis_url=PUBLIC), PUBLIC)
    assert fake.records[SIGNER] == {"account_seed": "kept", "member_swarms": {}}
    assert _scoped_line(fake) == []


def test_an_invite_whose_member_swarms_write_fails_revokes_the_new_member(admin):
    fake = Bao({SIGNER: {"member_swarms": {}}})
    request = invite.Request("lab", RECORD, url=URL, redis_url=PUBLIC, signer_path=SIGNER, swarms=("bad.name",))
    with pytest.raises(auth.HiveError) as error:
        invite.invite(admin, _bao(fake), request, PUBLIC)
    assert str(error.value).startswith("member_swarms contains an invalid swarm name")
    assert auth_members(admin) == []
    assert fake.records[SIGNER] == {"member_swarms": {}}


def auth_members(admin):
    return sorted(key.rsplit(":", 1)[1] for key in admin.keys(f"{auth.PREFIX}:member:*"))


def test_the_hive_endpoint_checks_membership_before_rotating_the_redis_password(admin):
    member = auth.mint(admin, "h1", PUBLIC)
    before = _password(admin, member["id"])
    with pytest.raises(auth.HiveError) as error:
        server._grant("refresh", admin, member["ledger_credential"], PUBLIC, _signer({"other": ("rig",)}))
    assert str(error.value) == (
        f"the hub record lists no member swarms for hive member {member['id']}; it mints no unscoped bus credential"
    )
    assert _password(admin, member["id"]) == before


def test_a_listed_member_still_rotates_and_gets_a_scoped_credential(admin):
    member = auth.mint(admin, "h1", PUBLIC)
    before = _password(admin, member["id"])
    grant = server._grant("refresh", admin, member["ledger_credential"], PUBLIC, _signer({member["id"]: ("rig",)}))
    assert grant["bus_scoped"] is True
    assert grant["id"] == member["id"]
    assert _password(admin, member["id"]) != before
    password = urllib.parse.urlsplit(grant["redis_url"]).password
    assert [hashlib.sha256(password.encode()).hexdigest()] == _password(admin, member["id"])


def test_an_unknown_credential_is_left_to_the_refresh_refusal(admin):
    with pytest.raises(auth.HiveError) as error:
        server._grant("refresh", admin, "unknown", PUBLIC, _signer({}))
    assert str(error.value) == "the ledger credential belongs to no hive member"


def test_the_member_lookup_reports_a_redis_failure(admin, monkeypatch):
    from redis.exceptions import ConnectionError as Unreachable

    def refuse(*args):
        raise Unreachable("down")

    monkeypatch.setattr(admin, "get", refuse)
    with pytest.raises(auth.HiveError) as error:
        auth.member_of(admin, "credential")
    assert str(error.value) == "Redis refused the member lookup (down)"
    assert isinstance(error.value.__cause__, Unreachable)


def test_the_hive_endpoint_reloads_its_signer_on_each_request(admin):
    member = auth.mint(admin, "h1", PUBLIC)
    loads = []

    def load():
        loads.append(True)
        return _signer({member["id"]: ("rig",)})

    from tests.hive.test_nats_grant import _refresh

    assert _refresh(admin, load, member["ledger_credential"])["bus_scoped"] is True
    assert _refresh(admin, load, member["ledger_credential"])["bus_scoped"] is True
    assert len(loads) == 2


def test_hive_scope_backfills_from_the_cli(monkeypatch, capsys):
    seen = []
    monkeypatch.setenv(openbao_kv.URL_ENV, BAO_URL)
    monkeypatch.setenv(openbao_kv.TOKEN_ENV, BAO_TOKEN)
    monkeypatch.setenv(nats_grant.SIGNER_ENV, SIGNER)
    monkeypatch.setattr(cli, "redis_client", lambda: "redis")
    monkeypatch.setattr(member_swarms, "backfill", lambda *args: seen.append(args) or "wrote")
    assert cli.main(["scope", "--swarm", "rig", "--swarm", "second"]) == 0
    ((redis, bao, path, named),) = seen
    assert (redis, bao.url, bao.token, path, named) == ("redis", BAO_URL, BAO_TOKEN, SIGNER, ["rig", "second"])
    assert capsys.readouterr().out == "wrote\n"


def test_hive_scope_refuses_without_a_signer_record(monkeypatch, capsys):
    monkeypatch.setenv(openbao_kv.URL_ENV, BAO_URL)
    monkeypatch.setenv(openbao_kv.TOKEN_ENV, BAO_TOKEN)
    monkeypatch.delenv(nats_grant.SIGNER_ENV, raising=False)
    assert cli.main(["scope"]) == 1
    assert capsys.readouterr().err == (
        "hive scope refused: OPENBAO_URL, OPENBAO_TOKEN and AGENTIHOOKS_NATS_SIGNER naming an OpenBao record "
        "under agentihooks/hub/ must all be set to list member swarms\n"
    )


def test_the_cli_invite_names_the_signer_record_and_its_swarms(monkeypatch, admin):
    seen = []
    admin.sadd(f"{SWARMS}:index", "rig")
    monkeypatch.setenv(openbao_kv.URL_ENV, BAO_URL)
    monkeypatch.setenv(openbao_kv.TOKEN_ENV, BAO_TOKEN)
    monkeypatch.setenv(nats_grant.SIGNER_ENV, SIGNER)
    monkeypatch.setattr(cli, "redis_client", lambda: admin)
    monkeypatch.setattr(cli.ledger_ca, "served", lambda: None)
    monkeypatch.setattr("scripts.swarm.store.redis_url", lambda environ: PUBLIC)
    monkeypatch.setattr(invite, "invite", lambda *args: seen.append(args) or "invited")
    assert cli.main(["invite", "lab", "--url", URL]) == 0
    assert cli.main(["invite", "lab", "--url", URL, "--swarm", "named"]) == 0
    assert [(args[2].signer_path, args[2].swarms) for args in seen] == [(SIGNER, ("rig",)), (SIGNER, ("named",))]


def test_the_cli_invite_with_a_file_signer_stays_unscoped(monkeypatch):
    seen = []
    monkeypatch.setenv(openbao_kv.URL_ENV, BAO_URL)
    monkeypatch.setenv(openbao_kv.TOKEN_ENV, BAO_TOKEN)
    monkeypatch.setenv(nats_grant.SIGNER_ENV, "file:/run/signer")
    monkeypatch.setattr(cli, "redis_client", lambda: "redis")
    monkeypatch.setattr(cli.ledger_ca, "served", lambda: None)
    monkeypatch.setattr("scripts.swarm.store.redis_url", lambda environ: PUBLIC)
    monkeypatch.setattr(invite, "invite", lambda *args: seen.append(args) or "invited")
    assert cli.main(["invite", "lab", "--url", URL]) == 0
    assert [(args[2].signer_path, args[2].swarms) for args in seen] == [(None, ())]
