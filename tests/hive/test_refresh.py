import hashlib
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from scripts.hive import auth, cli, registry, server
from scripts.swarm import store

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
    grant = auth.mint(admin, "laptop", PUBLIC)
    admin.acl_deluser(f"hive-{grant['id']}")
    admin.srem(auth.INDEX, grant["id"])

    refreshed = auth.refresh(admin, grant["ledger_credential"], PUBLIC)

    assert refreshed["id"] == grant["id"]
    assert admin.smembers(auth.INDEX) == {grant["id"]}
    assert refreshed["redis_url"].startswith(f"rediss://hive-{grant['id']}:")
    assert refreshed["redis_url"].endswith("@hive.example:6380/0")
    assert _accepts(admin, refreshed["redis_url"])
    user = admin.acl_getuser(f"hive-{grant['id']}")
    assert user["enabled"] is True
    assert f"~{auth.ROOT}:hive:{grant['id']}" in user["keys"]
    assert user["channels"] == [f"&{pattern}" for pattern in auth.ACL_PATTERNS]
    assert user["categories"] == list(auth.ACL_CATEGORIES)


def test_a_refresh_retires_the_previous_password(admin):
    grant = auth.mint(admin, "laptop", PUBLIC)

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
    grant = auth.mint(admin, "laptop", PUBLIC)
    auth.revoke(admin, grant["id"])

    with pytest.raises(auth.HiveError, match=f"^{NO_MEMBER}$"):
        auth.refresh(admin, grant["ledger_credential"], PUBLIC)

    assert admin.acl_users() == ["default"]


def test_a_redis_refusal_during_refresh_is_reported(admin, redis_lib, monkeypatch):
    grant = auth.mint(admin, "laptop", PUBLIC)

    def refuse(self, *args, **kwargs):
        raise redis_lib.exceptions.ResponseError("NOPERM")

    monkeypatch.setattr(redis_lib.client.Pipeline, "execute", refuse)

    with pytest.raises(auth.HiveError, match=r"^Redis refused the refreshed member \(NOPERM\)$"):
        auth.refresh(admin, grant["ledger_credential"], PUBLIC)


def _saved_users(aclfile):
    return sorted(line.split()[1] for line in aclfile.read_text().splitlines() if line.startswith("user "))


def test_join_refresh_and_revoke_save_the_acl_file(saved):
    redis, aclfile = saved

    grant = auth.mint(redis, "laptop", PUBLIC)
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
        auth.mint(redis, "laptop", PUBLIC)

    assert redis.acl_users() == ["default"]
    assert redis.keys(f"{auth.PREFIX}:member:*") == []
    assert redis.keys(f"{auth.PREFIX}:ledger:*") == []


def test_a_revoke_whose_acl_save_fails_is_reported_and_completes_on_retry(saved, redis_lib, monkeypatch):
    redis, aclfile = saved
    grant = auth.mint(redis, "laptop", PUBLIC)

    def refuse(self):
        raise redis_lib.exceptions.ResponseError("ERR There was an error trying to save the ACLs")

    with monkeypatch.context() as patched:
        patched.setattr(redis_lib.Redis, "acl_save", refuse)
        with pytest.raises(auth.HiveError) as refused:
            auth.revoke(redis, grant["id"])

    assert str(refused.value) == (
        f"revoking {grant['id']} is incomplete and a Redis restart before it completes revives its Redis user; "
        "run revoke again (ERR There was an error trying to save the ACLs)"
    )
    assert redis.acl_users() == ["default"]
    assert auth.ledger_member(redis, grant["ledger_credential"]) is None
    assert _saved_users(aclfile) == ["default", f"hive-{grant['id']}"]

    auth.revoke(redis, grant["id"])

    assert _saved_users(aclfile) == ["default"]
    assert redis.keys(f"{auth.PREFIX}:member:*") == []
    with pytest.raises(auth.HiveError, match=f"^no hive member {grant['id']}$"):
        auth.revoke(redis, grant["id"])


def test_the_refresh_endpoint_answers_with_the_refreshed_grant(admin, hive):
    grant = auth.mint(admin, "laptop", PUBLIC)
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
    grant = auth.mint(admin, "laptop", PUBLIC)
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


def test_cli_refresh_reads_its_hive_from_hive_env_alone(admin, hive, tmp_path, monkeypatch, capsys):
    grant = _joined(admin, hive, tmp_path, monkeypatch)
    for name in ("AGENTIHOOKS_HIVE_ID", "AGENTIHOOKS_HIVE_URL", "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL"):
        monkeypatch.delenv(name)
    monkeypatch.delenv("AGENTIHOOKS_HIVE_REDIS_URL")
    admin.acl_deluser(f"hive-{grant['id']}")

    assert cli.main(["refresh"]) == 0

    env = _env_file(tmp_path)
    assert capsys.readouterr().out.startswith(f"refreshed the Redis credential of hive member {grant['id']};")
    assert env["AGENTIHOOKS_HIVE_URL"] == hive
    assert env["AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL"] == grant["ledger_credential"]
    assert _accepts(admin, env["AGENTIHOOKS_HIVE_REDIS_URL"])


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


def _hive_environ(hive, grant, home):
    return {
        "AGENTIHOOKS_HOME": str(home),
        "AGENTIHOOKS_DEPLOYMENT": "distributed",
        "AGENTIHOOKS_HIVE_URL": hive,
        "AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL": grant["ledger_credential"],
        "AGENTIHOOKS_HIVE_REDIS_URL": grant["redis_url"],
    }


def test_a_refused_hive_credential_is_refreshed_once_and_reconnects(admin, hive, redis_lib, tmp_path, monkeypatch):
    grant = _joined(admin, hive, tmp_path, monkeypatch)
    admin.acl_deluser(f"hive-{grant['id']}")
    pinged = _fake_ping(admin, redis_lib, monkeypatch)
    environ = _hive_environ(hive, grant, tmp_path)

    client = store.redis_client(environ)

    assert pinged == [f"hive-{grant['id']}", f"hive-{grant['id']}"]
    assert environ["AGENTIHOOKS_HIVE_REDIS_URL"] == _env_file(tmp_path)["AGENTIHOOKS_HIVE_REDIS_URL"]
    assert environ["AGENTIHOOKS_HIVE_REDIS_URL"] != grant["redis_url"]
    assert client.connection_pool.connection_kwargs["password"] == _password(environ["AGENTIHOOKS_HIVE_REDIS_URL"])
    assert client.connection_pool.connection_class is redis_lib.connection.SSLConnection


def test_an_accepted_hive_credential_is_not_refreshed(admin, hive, redis_lib, tmp_path, monkeypatch):
    grant = _joined(admin, hive, tmp_path, monkeypatch)
    pinged = _fake_ping(admin, redis_lib, monkeypatch)
    environ = _hive_environ(hive, grant, tmp_path)

    store.redis_client(environ)

    assert pinged == [f"hive-{grant['id']}"]
    assert environ["AGENTIHOOKS_HIVE_REDIS_URL"] == grant["redis_url"]


def test_a_credential_another_process_refreshed_is_adopted_without_a_second_refresh(
    admin, hive, redis_lib, tmp_path, monkeypatch
):
    grant = _joined(admin, hive, tmp_path, monkeypatch)
    sibling = auth.refresh(admin, grant["ledger_credential"], PUBLIC)
    auth.write_env(tmp_path, hive, {**sibling, "ledger_credential": grant["ledger_credential"]})
    pinged = _fake_ping(admin, redis_lib, monkeypatch)
    environ = _hive_environ(hive, grant, tmp_path)

    client = store.redis_client(environ)

    assert pinged == [f"hive-{grant['id']}", f"hive-{grant['id']}"]
    assert environ["AGENTIHOOKS_HIVE_REDIS_URL"] == sibling["redis_url"]
    assert _env_file(tmp_path)["AGENTIHOOKS_HIVE_REDIS_URL"] == sibling["redis_url"]
    assert _accepts(admin, sibling["redis_url"])
    assert client.connection_pool.connection_kwargs["password"] == _password(sibling["redis_url"])


def test_a_saved_credential_that_is_also_refused_is_refreshed(admin, hive, redis_lib, tmp_path, monkeypatch):
    grant = _joined(admin, hive, tmp_path, monkeypatch)
    stale = auth.refresh(admin, grant["ledger_credential"], PUBLIC)
    auth.write_env(tmp_path, hive, {**stale, "ledger_credential": grant["ledger_credential"]})
    admin.acl_deluser(f"hive-{grant['id']}")
    pinged = _fake_ping(admin, redis_lib, monkeypatch)
    environ = _hive_environ(hive, grant, tmp_path)

    store.redis_client(environ)

    assert pinged == [f"hive-{grant['id']}"] * 3
    assert environ["AGENTIHOOKS_HIVE_REDIS_URL"] not in (grant["redis_url"], stale["redis_url"])
    assert _accepts(admin, environ["AGENTIHOOKS_HIVE_REDIS_URL"])


def test_two_processes_refused_at_once_refresh_once_and_share_the_credential(
    admin, hive, redis_lib, tmp_path, monkeypatch
):
    grant = _joined(admin, hive, tmp_path, monkeypatch)
    admin.acl_deluser(f"hive-{grant['id']}")
    both_refused = threading.Barrier(2)
    refreshes = []
    real_refresh = auth.refresh

    def counted(*args):
        refreshes.append(args[1])
        return real_refresh(*args)

    monkeypatch.setattr(auth, "refresh", counted)

    def ping(self):
        kwargs = self.connection_pool.connection_kwargs
        if kwargs["password"] == _password(grant["redis_url"]):
            both_refused.wait(timeout=5)
            raise redis_lib.exceptions.AuthenticationError("invalid username-password pair or user is disabled.")
        digest = hashlib.sha256(kwargs["password"].encode()).hexdigest()
        if digest not in admin.acl_getuser(kwargs["username"])["passwords"]:
            raise redis_lib.exceptions.AuthenticationError("invalid username-password pair or user is disabled.")
        return True

    monkeypatch.setattr(redis_lib.Redis, "ping", ping)
    environs = [_hive_environ(hive, grant, tmp_path) for _ in range(2)]
    clients, failures = [], []

    def connect(environ):
        try:
            clients.append(store.redis_client(environ))
        except Exception as exc:  # noqa: BLE001  surfaced through the failures assertion below
            failures.append(exc)

    threads = [threading.Thread(target=connect, args=(environ,)) for environ in environs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert failures == []
    assert len(refreshes) == 1
    assert environs[0]["AGENTIHOOKS_HIVE_REDIS_URL"] == environs[1]["AGENTIHOOKS_HIVE_REDIS_URL"]
    assert _accepts(admin, environs[0]["AGENTIHOOKS_HIVE_REDIS_URL"])
    assert {client.connection_pool.connection_kwargs["password"] for client in clients} == {
        _password(environs[0]["AGENTIHOOKS_HIVE_REDIS_URL"])
    }


@pytest.mark.parametrize("content", [None, "AGENTIHOOKS_HIVE_ID=a\n", "AGENTIHOOKS_HIVE_REDIS_URL=\n"])
def test_no_saved_hive_redis_url_reads_as_none(tmp_path, content):
    if content is not None:
        (tmp_path / "hive.env").write_text(content)

    assert cli.saved_redis_url({"AGENTIHOOKS_HOME": str(tmp_path)}) is None


def test_a_saved_hive_redis_url_keeps_every_equals_sign_after_the_name(tmp_path):
    url = auth._with_user("rediss://hive.example:6380/0?ssl_cert_reqs=none&x=y", "hive-a", "p")
    auth.write_env(tmp_path, "https://hive.example:8770", {"id": "a", "ledger_credential": "l", "redis_url": url})

    assert cli.saved_redis_url({"AGENTIHOOKS_HOME": str(tmp_path)}) == url


def test_the_refresh_command_is_listed_with_its_help():
    (commands,) = cli._parser()._subparsers._group_actions
    helps = {action.dest: action.help for action in commands._choices_actions}

    assert helps["refresh"] == "Mint a new Redis credential for this hive through its hive endpoint"


def test_the_hive_home_comes_from_the_mapping_then_the_process(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "process"))

    assert registry.home({"AGENTIHOOKS_HOME": str(tmp_path / "given")}) == tmp_path / "given"
    assert registry.home({}) == tmp_path / "process"
    monkeypatch.delenv("AGENTIHOOKS_HOME")
    assert registry.home({}) == Path.home() / ".agentihooks"


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
    environ = _hive_environ(hive, grant, tmp_path)

    with pytest.raises(redis_lib.exceptions.AuthenticationError) as refused:
        store.redis_client(environ)

    assert str(refused.value) == (f"the hive Redis credential was refused and the hive refused a refresh ({NO_MEMBER})")
    assert pinged == [f"hive-{grant['id']}"]
    assert environ["AGENTIHOOKS_HIVE_REDIS_URL"] == grant["redis_url"]


def test_a_member_the_process_env_left_behind_yields_to_the_saved_member(admin, hive, redis_lib, tmp_path, monkeypatch):
    left = auth.mint(admin, "pod-before-restart", PUBLIC)
    grant = _joined(admin, hive, tmp_path, monkeypatch)
    pinged = _fake_ping(admin, redis_lib, monkeypatch)
    environ = {
        **_hive_environ(hive, grant, tmp_path),
        "AGENTIHOOKS_HIVE_ID": left["id"],
        "AGENTIHOOKS_HIVE_REDIS_URL": left["redis_url"],
    }

    client = store.redis_client(environ)

    assert pinged == [f"hive-{grant['id']}"]
    assert environ["AGENTIHOOKS_HIVE_ID"] == grant["id"]
    assert environ["AGENTIHOOKS_HIVE_REDIS_URL"] == grant["redis_url"]
    assert client.connection_pool.connection_kwargs["username"] == f"hive-{grant['id']}"


def test_a_hive_process_without_a_member_in_its_env_reads_the_saved_member(
    admin, hive, redis_lib, tmp_path, monkeypatch
):
    grant = _joined(admin, hive, tmp_path, monkeypatch)
    pinged = _fake_ping(admin, redis_lib, monkeypatch)
    environ = {"AGENTIHOOKS_HOME": str(tmp_path), "AGENTIHOOKS_DEPLOYMENT": "distributed"}

    store.redis_client(environ)

    assert pinged == [f"hive-{grant['id']}"]
    assert environ["AGENTIHOOKS_HIVE_ID"] == grant["id"]
    assert environ["AGENTIHOOKS_HIVE_REDIS_URL"] == grant["redis_url"]


@pytest.mark.parametrize(
    ("lines", "member"),
    [
        (None, set()),
        (["AGENTIHOOKS_HIVE_ID=", "AGENTIHOOKS_HIVE_REDIS_URL="], set()),
        (["AGENTIHOOKS_HIVE_ID=a", "OTHER=b"], {"AGENTIHOOKS_HIVE_ID"}),
        (
            ["AGENTIHOOKS_HIVE_ID=a", "AGENTIHOOKS_HIVE_REDIS_URL={url}"],
            {"AGENTIHOOKS_HIVE_ID", "AGENTIHOOKS_HIVE_REDIS_URL"},
        ),
    ],
)
def test_the_saved_member_holds_only_its_set_id_and_redis_url(tmp_path, lines, member):
    url = auth._with_user("rediss://hive.example:6380/0?x=y", "hive-a", "p")
    if lines is not None:
        (tmp_path / "hive.env").write_text("".join(line.format(url=url) + "\n" for line in lines))

    saved = cli.saved_member({"AGENTIHOOKS_HOME": str(tmp_path)})

    assert set(saved) == member
    assert saved.get("AGENTIHOOKS_HIVE_ID", "a") == "a"
    assert saved.get("AGENTIHOOKS_HIVE_REDIS_URL", url) == url


def test_a_workstation_keeps_its_swarm_redis_beside_a_saved_hive_member(admin, hive, redis_lib, tmp_path, monkeypatch):
    _joined(admin, hive, tmp_path, monkeypatch)
    swarm_url = auth._with_user("redis://h:1/0", "swarm", "p")
    admin.acl_setuser("swarm", enabled=True, passwords=["+p"], keys=["*"], commands=["+@all"])
    pinged = _fake_ping(admin, redis_lib, monkeypatch)
    environ = {"AGENTIHOOKS_HOME": str(tmp_path), "AGENTIHOOKS_SWARM_REDIS_URL": swarm_url}

    store.redis_client(environ)

    assert pinged == ["swarm"]
    assert "AGENTIHOOKS_HIVE_REDIS_URL" not in environ
    assert "AGENTIHOOKS_HIVE_ID" not in environ
