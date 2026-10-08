import hashlib
import stat
import threading
import time

import fakeredis
import pytest
import redis as redis_lib

from scripts.hive import auth, cli, server
from scripts.swarm import store
from scripts.swarm.keyspace import ROOT

pytestmark = pytest.mark.xdist_group("fakeredis")

PUBLIC = "redis://hive.example:6380/2"


@pytest.fixture
def fake():
    return fakeredis.FakeServer()


@pytest.fixture
def admin(fake):
    return fakeredis.FakeRedis(server=fake, decode_responses=True)


def _as_user(fake, url):
    parts = redis_lib.connection.parse_url(url)
    return fakeredis.FakeRedis(
        server=fake, username=parts["username"], password=parts["password"], decode_responses=True
    )


def _invite_key(code):
    return f"{ROOT}-hive:invite:{hashlib.sha256(code.encode()).hexdigest()}"


def test_invite_stores_only_the_code_hash_for_fifteen_minutes(admin):
    code = auth.invite(admin, "laptop")

    assert admin.keys("*") == [_invite_key(code)]
    assert admin.get(_invite_key(code)) == "laptop"
    assert admin.ttl(_invite_key(code)) == 900
    assert code not in str(admin.keys("*"))


def test_a_valid_code_joins_with_a_ledger_credential_and_a_redis_acl_user(admin, fake):
    grant = auth.exchange(admin, auth.invite(admin, "laptop"), PUBLIC)

    assert grant["name"] == "laptop"
    assert grant["redis_url"].startswith(f"redis://hive-{grant['id']}:")
    assert grant["redis_url"].endswith("@hive.example:6380/2")
    assert auth.ledger_member(admin, grant["ledger_credential"]) == grant["id"]
    assert grant["ledger_credential"] not in str({key: admin.dump(key) for key in admin.keys("*")})
    user = admin.acl_getuser(f"hive-{grant['id']}")
    assert user["keys"] == [f"~{ROOT}:*"]
    assert user["channels"] == [f"&{ROOT}:*"]
    assert user["categories"] == ["+@all", "-@admin", "-@dangerous"]
    assert user["commands"] == []
    password = redis_lib.connection.parse_url(grant["redis_url"])["password"]
    assert user["passwords"] == [hashlib.sha256(password.encode()).hexdigest()]
    member = _as_user(fake, grant["redis_url"])
    assert member.set(f"{ROOT}:probe", "1") is True
    with pytest.raises(redis_lib.exceptions.NoPermissionError):
        member.set(f"{ROOT}-hive:invite:forged", "intruder")


def test_a_reused_code_is_refused(admin):
    code = auth.invite(admin, "laptop")
    auth.exchange(admin, code, PUBLIC)

    with pytest.raises(auth.HiveError, match="^invite code is invalid, expired or already used$"):
        auth.exchange(admin, code, PUBLIC)


def test_an_expired_code_is_refused(admin):
    code = auth.invite(admin, "laptop")
    admin.pexpire(_invite_key(code), 1)
    time.sleep(0.01)

    with pytest.raises(auth.HiveError, match="^invite code is invalid, expired or already used$"):
        auth.exchange(admin, code, PUBLIC)


def test_revoke_cuts_both_credentials(admin, fake):
    grant = auth.exchange(admin, auth.invite(admin, "laptop"), PUBLIC)
    other = auth.exchange(admin, auth.invite(admin, "desk"), PUBLIC)

    auth.revoke(admin, grant["id"])

    assert auth.ledger_member(admin, grant["ledger_credential"]) is None
    with pytest.raises(redis_lib.exceptions.AuthenticationError):
        _as_user(fake, grant["redis_url"]).ping()
    assert auth.members(admin) == {other["id"]: "desk"}
    assert auth.ledger_member(admin, other["ledger_credential"]) == other["id"]


def test_revoking_an_unknown_member_is_refused(admin):
    with pytest.raises(auth.HiveError, match="^no hive member nobody$"):
        auth.revoke(admin, "nobody")


def test_a_rediss_url_keeps_tls_and_its_query(admin):
    grant = auth.exchange(admin, auth.invite(admin, "laptop"), "rediss://hive.example:6380/0?ssl_ca_certs=/ca.pem")

    assert grant["redis_url"].startswith(f"rediss://hive-{grant['id']}:")
    assert grant["redis_url"].endswith("@hive.example:6380/0?ssl_ca_certs=/ca.pem")


def test_the_env_file_is_private_and_holds_both_credentials(tmp_path):
    grant = {"id": "ab12", "name": "laptop", "ledger_credential": "led", "redis_url": "redis://hive-ab12:pw@h:1/0"}

    path = auth.write_env(tmp_path / "home", "http://hive.example:8770", grant)

    assert path == tmp_path / "home" / "hive.env"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text() == (
        "AGENTIHOOKS_HIVE_ID=ab12\n"
        "AGENTIHOOKS_HIVE_URL=http://hive.example:8770\n"
        "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL=led\n"
        "AGENTIHOOKS_HIVE_REDIS_URL=redis://hive-ab12:pw@h:1/0\n"
    )


def test_rejoining_tightens_an_existing_env_file(tmp_path):
    (tmp_path / "hive.env").write_text("old\n")
    (tmp_path / "hive.env").chmod(0o644)
    grant = {"id": "ab12", "name": "laptop", "ledger_credential": "led", "redis_url": "redis://u:p@h:1/0"}

    auth.write_env(tmp_path, "http://hive", grant)

    assert stat.S_IMODE((tmp_path / "hive.env").stat().st_mode) == 0o600
    assert "old" not in (tmp_path / "hive.env").read_text()


@pytest.fixture
def hive(admin):
    httpd = server.make_server(admin, PUBLIC, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_join_over_http_writes_the_env_file_and_prints_no_secret(admin, hive, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    code = auth.invite(admin, "laptop")

    assert cli.main(["join", hive, code]) == 0

    out = capsys.readouterr().out
    env = dict(line.split("=", 1) for line in (tmp_path / "hive.env").read_text().splitlines())
    (member_id,) = auth.members(admin)
    assert out == f"joined the hive as {member_id}; credentials are in {tmp_path / 'hive.env'}\n"
    assert env["AGENTIHOOKS_HIVE_ID"] == member_id
    assert env["AGENTIHOOKS_HIVE_URL"] == hive
    assert auth.ledger_member(admin, env["AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL"]) == member_id
    assert env["AGENTIHOOKS_HIVE_REDIS_URL"].startswith(f"redis://hive-{member_id}:")
    assert env["AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL"] not in out
    assert env["AGENTIHOOKS_HIVE_REDIS_URL"] not in out


def test_join_over_http_with_a_used_code_is_refused(admin, hive, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    code = auth.invite(admin, "laptop")
    auth.exchange(admin, code, PUBLIC)

    assert cli.main(["join", hive, code]) == 1

    assert capsys.readouterr().err == "hive join refused: invite code is invalid, expired or already used\n"
    assert not (tmp_path / "hive.env").exists()


def test_the_join_endpoint_refuses_other_paths_and_bad_bodies(hive):
    import json
    import urllib.error
    import urllib.request

    for path, body in (("/other", b"{}"), ("/hive/join", b"not json"), ("/hive/join", b"{}")):
        request = urllib.request.Request(hive + path, data=body, method="POST")
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(request, timeout=5)
        assert refused.value.code == (404 if path == "/other" else 400)
        assert "error" in json.load(refused.value)


def test_cli_invite_list_and_revoke(admin, monkeypatch, capsys):
    monkeypatch.setattr(cli, "redis_client", lambda: admin)

    assert cli.main(["invite", "laptop"]) == 0
    code = capsys.readouterr().out.strip()
    grant = auth.exchange(admin, code, PUBLIC)
    assert cli.main(["list"]) == 0
    assert capsys.readouterr().out == f"{grant['id']}\tlaptop\n"
    assert cli.main(["revoke", grant["id"]]) == 0
    assert capsys.readouterr().out == f"revoked {grant['id']}\n"
    assert cli.main(["revoke", grant["id"]]) == 1
    assert capsys.readouterr().err == f"hive revoke refused: no hive member {grant['id']}\n"


@pytest.mark.parametrize(
    ("environ", "url"),
    [
        ({}, store.DEFAULT_URL),
        ({"AGENTIHOOKS_HIVE_REDIS_URL": "redis://hive-a:p@h:1/0"}, store.DEFAULT_URL),
        (
            {"AGENTIHOOKS_DEPLOYMENT": "local", "AGENTIHOOKS_HIVE_REDIS_URL": "redis://hive-a:p@h:1/0"},
            store.DEFAULT_URL,
        ),
        ({"AGENTIHOOKS_DEPLOYMENT": "compose", "AGENTIHOOKS_SWARM_REDIS_URL": "redis://r:6379/0"}, "redis://r:6379/0"),
        (
            {
                "AGENTIHOOKS_DEPLOYMENT": "compose",
                "AGENTIHOOKS_SWARM_REDIS_URL": "redis://r:6379/0",
                "AGENTIHOOKS_HIVE_REDIS_URL": "redis://hive-a:p@h:1/0",
            },
            "redis://hive-a:p@h:1/0",
        ),
        (
            {"AGENTIHOOKS_DEPLOYMENT": "distributed", "AGENTIHOOKS_HIVE_REDIS_URL": "rediss://hive-a:p@h:1/0"},
            "rediss://hive-a:p@h:1/0",
        ),
    ],
)
def test_redis_url_uses_the_hive_credential_only_outside_local_mode(environ, url):
    assert store.redis_url(environ) == url


def test_redis_client_connects_over_tls_for_a_rediss_url(monkeypatch):
    monkeypatch.setattr(redis_lib.Redis, "ping", lambda self: True)

    client = store.redis_client(
        {"AGENTIHOOKS_DEPLOYMENT": "distributed", "AGENTIHOOKS_HIVE_REDIS_URL": "rediss://hive-a:p@h:1/0"}
    )

    assert client.connection_pool.connection_class is redis_lib.connection.SSLConnection
    assert client.connection_pool.connection_kwargs["username"] == "hive-a"
