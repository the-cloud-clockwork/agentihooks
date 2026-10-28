import hashlib
import io
import json
import os
import urllib.parse

import pytest

from scripts import openbao_kv
from scripts.hive import auth, cli, invite
from tests.codex_fakes import BAO_TOKEN, BAO_URL, http_error

pytestmark = pytest.mark.unit

LOCAL = "redis://127.0.0.1:6379/0"
PUBLIC = "rediss://hive.example:6380/2"
URL = "https://hive.example:8770"
SWARM = "https://ledger.example:8765"
CA = "-----BEGIN CERTIFICATE-----\nanton\n-----END CERTIFICATE-----\n"
LIKE = "k8s/agentihooks-swarm-workstation-hive"
RECORD = "k8s/agentihooks-hive-lab"


class Bao:
    def __init__(self, records=None, fail_writes=False):
        self.records = {path: dict(data) for path, data in (records or {}).items()}
        self.versions = dict.fromkeys(self.records, 1)
        self.fail_writes = fail_writes
        self.written = []

    def __call__(self, request, timeout=None):
        path = request.full_url.removeprefix(f"{BAO_URL}/v1/secret/data/")
        if request.get_method() == "GET":
            if path not in self.records:
                raise http_error(request.full_url, 404, b'{"errors": []}')
            body = {"data": {"data": self.records[path], "metadata": {"version": self.versions[path]}}}
            return io.BytesIO(json.dumps(body).encode())
        if self.fail_writes:
            raise http_error(request.full_url, 503, b'{"errors": ["sealed"]}')
        self.records[path] = json.loads(request.data)["data"]
        self.versions[path] = self.versions.get(path, 0) + 1
        self.written.append(path)
        return io.BytesIO(json.dumps({"data": {"version": self.versions[path]}}).encode())


@pytest.fixture
def admin():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


def _bao(fake):
    return openbao_kv.OpenBao(BAO_URL, BAO_TOKEN, fake)


def _credential(member_id="0123456789abcdef"):
    return (
        f"AGENTIHOOKS_HIVE_ID={member_id}\nAGENTIHOOKS_HIVE_URL={URL}\n"
        f"AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL=old\nAGENTIHOOKS_HIVE_REDIS_URL={PUBLIC}\n"
    )


def _sample():
    return {"credential": _credential(), "swarm_url": SWARM, "ca_pem": CA, "ssh_host_key": "kept"}


def _env(text):
    return dict(line.split("=", 1) for line in text.splitlines())


def _redis(admin, env):
    parts = urllib.parse.urlsplit(env["AGENTIHOOKS_HIVE_REDIS_URL"])
    assert hashlib.sha256(parts.password.encode()).hexdigest() in admin.acl_getuser(parts.username)["passwords"]
    return parts.scheme, parts.username, parts.hostname, parts.port, parts.path


def _members(admin):
    return sorted(auth.members(admin))


def test_the_default_record_is_named_after_the_label():
    assert invite.default_path("lab") == "k8s/agentihooks-hive-lab"


def test_like_copies_the_addresses_and_ca_of_another_hive_and_mints_a_new_member(admin):
    fake = Bao({LIKE: _sample()})

    message = invite.invite(admin, _bao(fake), invite.Request("lab", RECORD, like=LIKE), LOCAL)

    [member] = _members(admin)
    record = fake.records[RECORD]
    assert sorted(record) == ["ca_pem", "credential", "swarm_url"]
    assert record["swarm_url"] == SWARM
    assert record["ca_pem"] == CA
    env = _env(record["credential"])
    assert list(env) == [
        "AGENTIHOOKS_HIVE_ID",
        "AGENTIHOOKS_HIVE_URL",
        "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL",
        "AGENTIHOOKS_HIVE_REDIS_URL",
        "AGENTIHOOKS_SWARM_URL",
    ]
    assert env["AGENTIHOOKS_HIVE_ID"] == member
    assert env["AGENTIHOOKS_HIVE_URL"] == URL
    assert _redis(admin, env) == ("rediss", f"hive-{member}", "hive.example", 6380, "/2")
    assert auth.ledger_member(admin, env["AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL"]) == member
    assert auth.member(admin, member)["name"] == "lab"
    assert fake.records[LIKE] == _sample()
    assert message == (
        f"invited hive member {member} as lab; OpenBao secret/{RECORD}: wrote ca_pem, credential, swarm_url "
        "(3 keys total)"
    )


def test_flags_win_over_the_like_record_except_the_ca(admin):
    fake = Bao({LIKE: _sample()})
    request = invite.Request(
        "lab", RECORD, LIKE, "https://other:8770", "redis://other:6379/1", "https://other:8765", "local-ca"
    )

    invite.invite(admin, _bao(fake), request, LOCAL)

    record = fake.records[RECORD]
    env = _env(record["credential"])
    assert env["AGENTIHOOKS_HIVE_URL"] == "https://other:8770"
    assert _redis(admin, env) == ("redis", f"hive-{env['AGENTIHOOKS_HIVE_ID']}", "other", 6379, "/1")
    assert record["swarm_url"] == "https://other:8765"
    assert record["ca_pem"] == CA


def test_without_like_the_flags_and_this_hosts_redis_and_ca_fill_the_record(admin):
    fake = Bao()

    invite.invite(admin, _bao(fake), invite.Request("lab", RECORD, url=URL, ledger_ca="local-ca"), LOCAL)

    record = fake.records[RECORD]
    assert sorted(record) == ["ca_pem", "credential"]
    assert record["ca_pem"] == "local-ca"
    env = _env(record["credential"])
    assert _redis(admin, env) == ("redis", f"hive-{env['AGENTIHOOKS_HIVE_ID']}", "127.0.0.1", 6379, "/0")


def test_one_invite_carries_the_member_event_bus_address(admin):
    fake = Bao()
    request = invite.Request("lab", RECORD, url=URL, swarm_url=SWARM, bus_url="nats://bus.example.test:4222")
    invite.invite(admin, _bao(fake), request, LOCAL)
    env = _env(fake.records[RECORD]["credential"])
    assert env["AGENTIHOOKS_BUS"] == "nats"
    assert env["AGENTIHOOKS_BUS_URL"] == "nats://bus.example.test:4222"
    assert env["AGENTIHOOKS_SWARM_URL"] == SWARM


def test_like_copies_the_member_event_bus_address_and_an_explicit_address_wins(admin):
    sample = _sample()
    sample["credential"] += "AGENTIHOOKS_BUS=nats\nAGENTIHOOKS_BUS_URL=nats://copied.example.test:4222\n"
    fake = Bao({LIKE: sample})
    invite.invite(admin, _bao(fake), invite.Request("lab", RECORD, like=LIKE), LOCAL)
    assert _env(fake.records[RECORD]["credential"])["AGENTIHOOKS_BUS_URL"] == "nats://copied.example.test:4222"
    invite.invite(
        admin, _bao(fake), invite.Request("lab", RECORD, like=LIKE, bus_url="nats://override.example.test:4222"), LOCAL
    )
    env = _env(fake.records[RECORD]["credential"])
    assert env["AGENTIHOOKS_BUS"] == "nats"
    assert env["AGENTIHOOKS_BUS_URL"] == "nats://override.example.test:4222"


def test_a_like_record_without_a_credential_still_lends_its_ledger_address(admin):
    fake = Bao({LIKE: {"swarm_url": SWARM, "ca_pem": CA}})

    invite.invite(admin, _bao(fake), invite.Request("lab", RECORD, like=LIKE, url=URL), LOCAL)

    record = fake.records[RECORD]
    env = _env(record["credential"])
    assert env["AGENTIHOOKS_HIVE_URL"] == URL
    assert _redis(admin, env) == ("redis", f"hive-{env['AGENTIHOOKS_HIVE_ID']}", "127.0.0.1", 6379, "/0")
    assert record["swarm_url"] == SWARM


def test_a_like_redis_url_keeps_the_query_after_its_first_equals_sign(admin):
    sample = _sample()
    sample["credential"] = sample["credential"].replace(PUBLIC, f"{PUBLIC}?ssl_cert_reqs=required")
    fake = Bao({LIKE: sample})

    invite.invite(admin, _bao(fake), invite.Request("lab", RECORD, like=LIKE), LOCAL)

    url = _env(fake.records[RECORD]["credential"])["AGENTIHOOKS_HIVE_REDIS_URL"]
    assert urllib.parse.urlsplit(url).query == "ssl_cert_reqs=required"


def test_the_invite_parser_names_its_record_and_addresses():
    sub = next(action for action in cli._parser()._actions if action.dest == "command")
    helps = {choice.dest: choice.help for choice in sub._choices_actions}
    assert helps["invite"] == "Mint a hive member and write its credential into an OpenBao record"
    assert [(action.option_strings, action.dest, action.help) for action in sub.choices["invite"]._actions[1:]] == [
        ([], "label", "hive member name"),
        (["--path"], "path", "OpenBao record under the secret mount; defaults to k8s/agentihooks-hive-LABEL"),
        (["--like"], "like", "OpenBao record of another hive to copy addresses and the ledger CA from"),
        (["--url"], "url", "this hive's endpoint as the new hive reaches it, for hive refresh"),
        (["--redis-url"], "redis_url", "Redis URL the new hive connects to"),
        (["--swarm-url"], "swarm_url", "ledger address the new hive reaches"),
        (["--bus-url"], "bus_url", "NATS address the new hive reaches; copied from --like when omitted"),
        (["--swarm"], "swarm", "swarm the member's bus credential reaches; every swarm if omitted"),
    ]


def test_an_unknown_hive_url_is_refused_before_any_member_is_minted(admin):
    fake = Bao()

    with pytest.raises(auth.HiveError) as refused:
        invite.invite(admin, _bao(fake), invite.Request("lab", RECORD), LOCAL)

    assert str(refused.value) == "the hive URL is unknown: pass --url or --like with a hive record"
    assert _members(admin) == []
    assert fake.written == []


def test_a_missing_like_record_is_refused(admin):
    with pytest.raises(auth.HiveError) as refused:
        invite.invite(admin, _bao(Bao()), invite.Request("lab", RECORD, like="k8s/none", url=URL), LOCAL)

    assert str(refused.value) == "the record k8s/none that --like names is empty or missing"
    assert _members(admin) == []


def test_a_failed_write_revokes_the_new_member(admin):
    fake = Bao({LIKE: _sample()}, fail_writes=True)

    with pytest.raises(auth.HiveError) as refused:
        invite.invite(admin, _bao(fake), invite.Request("lab", RECORD, like=LIKE), LOCAL)

    assert str(refused.value) == "OpenBao HTTP 503: sealed"
    assert _members(admin) == []
    assert admin.smembers(auth.INDEX) == set()


def test_a_failed_write_whose_revoke_fails_reports_both(admin, monkeypatch):
    fake = Bao({LIKE: _sample()}, fail_writes=True)

    def refuse(redis, member_id):
        raise auth.HiveError(f"no hive member {member_id}")

    monkeypatch.setattr(auth, "revoke", refuse)

    with pytest.raises(auth.HiveError) as refused:
        invite.invite(admin, _bao(fake), invite.Request("lab", RECORD, like=LIKE), LOCAL)

    [member] = _members(admin)
    assert str(refused.value) == (
        f"OpenBao HTTP 503: sealed; the new member {member} was not revoked (no hive member {member})"
    )


def test_a_failed_read_is_refused_with_the_openbao_status(admin):
    def opener(request, timeout=None):
        raise http_error(request.full_url, 403, b'{"errors": ["permission denied"]}')

    with pytest.raises(auth.HiveError) as refused:
        invite.invite(admin, _bao(opener), invite.Request("lab", RECORD, url=URL), LOCAL)

    assert str(refused.value) == "OpenBao HTTP 403: permission denied"
    assert _members(admin) == []


def test_a_rerun_replaces_the_member_and_revokes_the_previous_one(admin):
    fake = Bao({LIKE: _sample()})
    request = invite.Request("lab", RECORD, like=LIKE)
    invite.invite(admin, _bao(fake), request, LOCAL)
    [first] = _members(admin)

    message = invite.invite(admin, _bao(fake), request, LOCAL)

    [second] = _members(admin)
    assert second != first
    assert _env(fake.records[RECORD]["credential"])["AGENTIHOOKS_HIVE_ID"] == second
    assert message == (
        f"invited hive member {second} as lab; OpenBao secret/{RECORD}: wrote ca_pem, credential, swarm_url "
        f"(3 keys total); revoked the previous member {first}"
    )


def test_a_previous_member_already_gone_is_reported_and_the_invite_stands(admin):
    fake = Bao({LIKE: _sample(), RECORD: {"credential": _credential("feedfacefeedface"), "extra": "kept"}})

    message = invite.invite(admin, _bao(fake), invite.Request("lab", RECORD, like=LIKE), LOCAL)

    [member] = _members(admin)
    assert fake.records[RECORD]["extra"] == "kept"
    assert message == (
        f"invited hive member {member} as lab; OpenBao secret/{RECORD}: wrote ca_pem, credential, swarm_url "
        "(4 keys total); the previous member was not revoked (no hive member feedfacefeedface)"
    )


def test_the_cli_passes_its_flags_and_prints_the_outcome(monkeypatch, capsys):
    seen = []
    monkeypatch.setenv("OPENBAO_URL", BAO_URL)
    monkeypatch.setenv("OPENBAO_TOKEN", BAO_TOKEN)
    monkeypatch.setattr(cli, "redis_client", lambda: "redis")
    monkeypatch.setattr(cli.ledger_ca, "served", lambda: "served-ca")
    monkeypatch.setattr("scripts.swarm.store.redis_url", lambda environ: LOCAL if environ is os.environ else None)
    monkeypatch.setattr(invite, "invite", lambda *args: seen.append(args) or "invited")

    assert (
        cli.main(
            [
                "invite",
                "lab",
                "--like",
                LIKE,
                "--url",
                URL,
                "--redis-url",
                PUBLIC,
                "--swarm-url",
                SWARM,
                "--bus-url",
                "nats://bus.example.test:4222",
            ]
        )
        == 0
    )
    assert cli.main(["invite", "lab2", "--path", "k8s/elsewhere"]) == 0

    assert capsys.readouterr().out == "invited\ninvited\n"
    [(redis, bao, first, local), (_, _, second, _)] = seen
    assert (redis, local) == ("redis", LOCAL)
    assert (bao.url, bao.token) == (BAO_URL, BAO_TOKEN)
    assert first == invite.Request(
        "lab", "k8s/agentihooks-hive-lab", LIKE, URL, PUBLIC, SWARM, "served-ca", bus_url="nats://bus.example.test:4222"
    )
    assert second == invite.Request("lab2", "k8s/elsewhere", None, None, None, None, "served-ca")


def test_the_cli_refuses_without_openbao_credentials(monkeypatch, capsys):
    monkeypatch.delenv("OPENBAO_URL", raising=False)
    monkeypatch.setenv("OPENBAO_TOKEN", BAO_TOKEN)

    assert cli.main(["invite", "lab"]) == 1
    assert capsys.readouterr().err == "hive invite refused: OPENBAO_URL and OPENBAO_TOKEN must both be set\n"
