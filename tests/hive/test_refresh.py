import hashlib
import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from scripts.hive import auth, cli, server
from scripts.swarm import store

pytestmark = pytest.mark.xdist_group("fakeredis")

PUBLIC = "rediss://hive.example:6380/0"
BAD_REFRESH_BODY = "the body must be a JSON object with a string ledger_credential"
NO_MEMBER = "the ledger credential belongs to no hive member"


@pytest.fixture
def redis_lib():
    import redis

    return redis


@pytest.fixture
def admin():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


@pytest.fixture
def saved(tmp_path):
    import fakeredis

    aclfile = tmp_path / "users.acl"
    server_ = fakeredis.FakeServer(config={b"aclfile": str(aclfile).encode()})
    return fakeredis.FakeRedis(server=server_, decode_responses=True), aclfile


@pytest.fixture
def hive(admin):
    httpd = server.make_server(admin, PUBLIC, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _password(url):
    return urllib.parse.urlsplit(url).password


def _accepts(redis, url):
    parts = urllib.parse.urlsplit(url)
    digest = hashlib.sha256(parts.password.encode()).hexdigest()
    return digest in redis.acl_getuser(parts.username)["passwords"]


def test_a_ledger_credential_recreates_a_redis_user_lost_on_restart(admin):
    grant = auth.exchange(admin, auth.invite(admin, "laptop"), PUBLIC)
    admin.acl_deluser(f"hive-{grant['id']}")

    refreshed = auth.refresh(admin, grant["ledger_credential"], PUBLIC)

    assert refreshed["id"] == grant["id"]
    assert refreshed["redis_url"].startswith(f"rediss://hive-{grant['id']}:")
    assert refreshed["redis_url"].endswith("@hive.example:6380/0")
    assert _accepts(admin, refreshed["redis_url"])
    user = admin.acl_getuser(f"hive-{grant['id']}")
    assert user["enabled"] is True
    assert user["keys"] == [f"~{pattern}" for pattern in auth.ACL_PATTERNS]
    assert user["channels"] == [f"&{pattern}" for pattern in auth.ACL_PATTERNS]
    assert user["categories"] == list(auth.ACL_CATEGORIES)


def test_a_refresh_retires_the_previous_password(admin):
    grant = auth.exchange(admin, auth.invite(admin, "laptop"), PUBLIC)

    refreshed = auth.refresh(admin, grant["ledger_credential"], PUBLIC)

    assert _password(refreshed["redis_url"]) != _password(grant["redis_url"])
    assert len(admin.acl_getuser(f"hive-{grant['id']}")["passwords"]) == 1
    assert not _accepts(admin, grant["redis_url"])
    assert _accepts(admin, refreshed["redis_url"])


@pytest.mark.parametrize("credential", ["", "not-a-member"])
def test_an_unknown_ledger_credential_is_refused(admin, credential):
    with pytest.raises(auth.HiveError, match=f"^{NO_MEMBER}$"):
        auth.refresh(admin, credential, PUBLIC)

    assert admin.acl_users() == ["default"]


def test_a_revoked_member_cannot_refresh(admin):
    grant = auth.exchange(admin, auth.invite(admin, "laptop"), PUBLIC)
    auth.revoke(admin, grant["id"])

    with pytest.raises(auth.HiveError, match=f"^{NO_MEMBER}$"):
        auth.refresh(admin, grant["ledger_credential"], PUBLIC)

    assert admin.acl_users() == ["default"]


def test_a_redis_refusal_during_refresh_is_reported(admin, redis_lib, monkeypatch):
    grant = auth.exchange(admin, auth.invite(admin, "laptop"), PUBLIC)

    def refuse(self, *args, **kwargs):
        raise redis_lib.exceptions.ResponseError("NOPERM")

    monkeypatch.setattr(redis_lib.client.Pipeline, "execute", refuse)

    with pytest.raises(auth.HiveError, match=r"^Redis refused the refreshed member \(NOPERM\)$"):
        auth.refresh(admin, grant["ledger_credential"], PUBLIC)


def _saved_users(aclfile):
    return sorted(line.split()[1] for line in aclfile.read_text().splitlines() if line.startswith("user "))


def test_join_refresh_and_revoke_save_the_acl_file(saved):
    redis, aclfile = saved

    grant = auth.exchange(redis, auth.invite(redis, "laptop"), PUBLIC)
    assert _saved_users(aclfile) == ["default", f"hive-{grant['id']}"]

    aclfile.write_text("")
    refreshed = auth.refresh(redis, grant["ledger_credential"], PUBLIC)
    digest = hashlib.sha256(_password(refreshed["redis_url"]).encode()).hexdigest()
    assert _saved_users(aclfile) == ["default", f"hive-{grant['id']}"]
    assert f"#{digest}" in aclfile.read_text()

    auth.revoke(redis, grant["id"])
    assert _saved_users(aclfile) == ["default"]


def test_a_failed_acl_save_undoes_the_join(saved, redis_lib, monkeypatch):
    redis, aclfile = saved

    def refuse(self):
        raise redis_lib.exceptions.ResponseError("ERR There was an error trying to save the ACLs")

    monkeypatch.setattr(redis_lib.Redis, "acl_save", refuse)

    with pytest.raises(auth.HiveError, match=r"^Redis refused the new member \(.*save the ACLs\)$"):
        auth.exchange(redis, auth.invite(redis, "laptop"), PUBLIC)

    assert redis.acl_users() == ["default"]
    assert redis.keys(f"{auth.PREFIX}:member:*") == []
    assert redis.keys(f"{auth.PREFIX}:ledger:*") == []


def test_a_failed_acl_save_after_a_revoke_is_reported(saved, redis_lib, monkeypatch):
    redis, aclfile = saved
    grant = auth.exchange(redis, auth.invite(redis, "laptop"), PUBLIC)

    def refuse(self):
        raise redis_lib.exceptions.ResponseError("ERR There was an error trying to save the ACLs")

    monkeypatch.setattr(redis_lib.Redis, "acl_save", refuse)

    with pytest.raises(auth.HiveError) as refused:
        auth.revoke(redis, grant["id"])

    assert str(refused.value) == (
        f"revoked {grant['id']}, but Redis could not save its ACL file, so a restart revives it "
        "(ERR There was an error trying to save the ACLs)"
    )
    assert redis.acl_users() == ["default"]
    assert _saved_users(aclfile) == ["default", f"hive-{grant['id']}"]


def test_the_refresh_endpoint_answers_with_the_refreshed_grant(admin, hive):
    grant = auth.exchange(admin, auth.invite(admin, "laptop"), PUBLIC)
    body = json.dumps({"ledger_credential": grant["ledger_credential"]}).encode()

    with urllib.request.urlopen(urllib.request.Request(hive + "/hive/refresh", data=body), timeout=5) as response:
        answer = json.load(response)

    assert sorted(answer) == ["id", "redis_url"]
    assert answer["id"] == grant["id"]
    assert _accepts(admin, answer["redis_url"])


@pytest.mark.parametrize(
    ("body", "status", "error"),
    [
        (b'{"ledger_credential": "nobody"}', 403, NO_MEMBER),
        (b'{"code": "x"}', 400, BAD_REFRESH_BODY),
        (b'{"ledger_credential": 1}', 400, BAD_REFRESH_BODY),
        (b"not json", 400, BAD_REFRESH_BODY),
    ],
)
def test_the_refresh_endpoint_refuses_bad_credentials_and_bodies(hive, body, status, error):
    request = urllib.request.Request(hive + "/hive/refresh", data=body, method="POST")

    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request, timeout=5)

    assert refused.value.code == status
    assert json.load(refused.value) == {"error": error}


def _joined(admin, hive, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    grant = auth.exchange(admin, auth.invite(admin, "laptop"), PUBLIC)
    auth.write_env(tmp_path, hive, grant)
    monkeypatch.setenv("AGENTIHOOKS_HIVE_ID", grant["id"])
    monkeypatch.setenv("AGENTIHOOKS_HIVE_URL", hive)
    monkeypatch.setenv("AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL", grant["ledger_credential"])
    monkeypatch.setenv("AGENTIHOOKS_HIVE_REDIS_URL", grant["redis_url"])
    return grant


def _env_file(tmp_path):
    return dict(line.split("=", 1) for line in (tmp_path / "hive.env").read_text().splitlines())


def test_cli_refresh_rewrites_hive_env_through_the_join_service(admin, hive, tmp_path, monkeypatch, capsys):
    grant = _joined(admin, hive, tmp_path, monkeypatch)
    admin.acl_deluser(f"hive-{grant['id']}")

    assert cli.main(["refresh"]) == 0

    out = capsys.readouterr().out
    env = _env_file(tmp_path)
    assert out == f"refreshed the Redis credential of hive member {grant['id']}; it is in {tmp_path / 'hive.env'}\n"
    assert env["AGENTIHOOKS_HIVE_ID"] == grant["id"]
    assert env["AGENTIHOOKS_HIVE_URL"] == hive
    assert env["AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL"] == grant["ledger_credential"]
    assert env["AGENTIHOOKS_HIVE_REDIS_URL"] != grant["redis_url"]
    assert _accepts(admin, env["AGENTIHOOKS_HIVE_REDIS_URL"])
    assert _password(env["AGENTIHOOKS_HIVE_REDIS_URL"]) not in out


@pytest.mark.parametrize("unset", ["AGENTIHOOKS_HIVE_URL", "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL"])
def test_cli_refresh_without_a_joined_hive_is_refused(tmp_path, monkeypatch, capsys, unset):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIHOOKS_HIVE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL", "led")
    monkeypatch.delenv(unset)

    assert cli.main(["refresh"]) == 1

    assert capsys.readouterr().err == (
        "hive refresh refused: this host has not joined a hive: "
        "AGENTIHOOKS_HIVE_URL and AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL must both be set\n"
    )
    assert not (tmp_path / "hive.env").exists()


def test_cli_refresh_refuses_plain_http_off_loopback(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIHOOKS_HIVE_URL", "http://hive.example:8770")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL", "led")

    assert cli.main(["refresh"]) == 1

    assert capsys.readouterr().err == (
        "hive refresh refused: a refresh off loopback carries credentials, so the hive URL must be https\n"
    )


def _fake_ping(admin, redis_lib, monkeypatch):
    pinged = []

    def ping(self):
        kwargs = self.connection_pool.connection_kwargs
        pinged.append(kwargs["username"])
        digest = hashlib.sha256((kwargs["password"] or "").encode()).hexdigest()
        known = kwargs["username"] in admin.acl_users()
        if not known or digest not in admin.acl_getuser(kwargs["username"])["passwords"]:
            raise redis_lib.exceptions.AuthenticationError("invalid username-password pair or user is disabled.")
        return True

    monkeypatch.setattr(redis_lib.Redis, "ping", ping)
    return pinged


def _hive_environ(hive, grant):
    return {
        "AGENTIHOOKS_DEPLOYMENT": "distributed",
        "AGENTIHOOKS_HIVE_URL": hive,
        "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL": grant["ledger_credential"],
        "AGENTIHOOKS_HIVE_REDIS_URL": grant["redis_url"],
    }


def test_a_refused_hive_credential_is_refreshed_once_and_reconnects(admin, hive, redis_lib, tmp_path, monkeypatch):
    grant = _joined(admin, hive, tmp_path, monkeypatch)
    admin.acl_deluser(f"hive-{grant['id']}")
    pinged = _fake_ping(admin, redis_lib, monkeypatch)
    environ = _hive_environ(hive, grant)

    client = store.redis_client(environ)

    assert pinged == [f"hive-{grant['id']}", f"hive-{grant['id']}"]
    assert environ["AGENTIHOOKS_HIVE_REDIS_URL"] == _env_file(tmp_path)["AGENTIHOOKS_HIVE_REDIS_URL"]
    assert environ["AGENTIHOOKS_HIVE_REDIS_URL"] != grant["redis_url"]
    assert client.connection_pool.connection_kwargs["password"] == _password(environ["AGENTIHOOKS_HIVE_REDIS_URL"])
    assert client.connection_pool.connection_class is redis_lib.connection.SSLConnection


def test_an_accepted_hive_credential_is_not_refreshed(admin, hive, redis_lib, tmp_path, monkeypatch):
    grant = _joined(admin, hive, tmp_path, monkeypatch)
    pinged = _fake_ping(admin, redis_lib, monkeypatch)
    environ = _hive_environ(hive, grant)

    store.redis_client(environ)

    assert pinged == [f"hive-{grant['id']}"]
    assert environ["AGENTIHOOKS_HIVE_REDIS_URL"] == grant["redis_url"]


def test_a_refused_swarm_credential_is_not_refreshed(admin, redis_lib, monkeypatch):
    pinged = _fake_ping(admin, redis_lib, monkeypatch)
    swarm_url = auth._with_user("redis://h:1/0", "swarm", "p")
    environ = {"AGENTIHOOKS_DEPLOYMENT": "compose", "AGENTIHOOKS_SWARM_REDIS_URL": swarm_url}

    with pytest.raises(redis_lib.exceptions.AuthenticationError, match="^invalid username-password pair"):
        store.redis_client(environ)

    assert pinged == ["swarm"]


def test_a_refused_refresh_reports_both_refusals(admin, hive, redis_lib, tmp_path, monkeypatch):
    grant = _joined(admin, hive, tmp_path, monkeypatch)
    auth.revoke(admin, grant["id"])
    pinged = _fake_ping(admin, redis_lib, monkeypatch)
    environ = _hive_environ(hive, grant)

    with pytest.raises(redis_lib.exceptions.AuthenticationError) as refused:
        store.redis_client(environ)

    assert str(refused.value) == (f"the hive Redis credential was refused and the hive refused a refresh ({NO_MEMBER})")
    assert pinged == [f"hive-{grant['id']}"]
    assert environ["AGENTIHOOKS_HIVE_REDIS_URL"] == grant["redis_url"]
