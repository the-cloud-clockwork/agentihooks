import hashlib
import io
import json
import os
import socket
import ssl
import stat
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

from scripts.hive import auth, cli, server
from scripts.swarm import store
from scripts.swarm.keyspace import ROOT

PUBLIC = "redis://hive.example:6380/2"


@pytest.fixture
def redis_lib():
    import redis

    return redis


@pytest.fixture
def admin():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


def test_a_minted_member_has_a_ledger_credential_and_a_redis_acl_user(admin, redis_lib):
    grant = auth.mint(admin, "laptop", PUBLIC)

    assert grant["name"] == "laptop"
    assert grant["redis_url"].startswith(f"redis://hive-{grant['id']}:")
    assert grant["redis_url"].endswith("@hive.example:6380/2")
    assert auth.ledger_member(admin, grant["ledger_credential"]) == grant["id"]
    assert grant["ledger_credential"] not in str({key: admin.dump(key) for key in admin.keys("*")})
    user = admin.acl_getuser(f"hive-{grant['id']}")
    assert f"~{ROOT}:hive:{grant['id']}" in user["keys"]
    assert admin.smembers(f"{ROOT}:hives") == {grant["id"]}
    assert user["channels"] == [f"&{ROOT}:*"]
    assert user["categories"] == ["+@all", "-@admin", "-@dangerous"]
    assert user["commands"] == []
    password = redis_lib.connection.parse_url(grant["redis_url"])["password"]
    assert user["passwords"] == [hashlib.sha256(password.encode()).hexdigest()]
    assert not auth.PREFIX.startswith(f"{ROOT}:")


def test_the_member_user_reads_every_root_key_and_writes_only_its_granted_patterns():
    calls = []

    class Recorder:
        def acl_setuser(self, *args, **kwargs):
            calls.append((args, kwargs))

    auth._set_user(Recorder(), "m1", "pw")

    assert calls == [
        (
            ("hive-m1",),
            {
                "enabled": True,
                "reset": True,
                "hashed_passwords": [f"+{hashlib.sha256(b'pw').hexdigest()}"],
                "categories": ("+@all", "-@admin", "-@dangerous"),
                "keys": (
                    f"%R~{ROOT}:*",
                    f"~{ROOT}:hive:m1",
                    f"~{ROOT}:hive:m1:*",
                    f"~{ROOT}:swarm:state:hive-m1:*",
                    f"~{ROOT}:swarm:*:agents",
                    f"~{ROOT}:swarm:*:executions",
                    f"~{ROOT}:swarm:*:execution-publishing",
                    f"~{ROOT}:swarm:*:codex-transport",
                    f"~{ROOT}:swarm:*:codex-endpoints",
                    f"~{ROOT}:swarm:*:codex-openings",
                    f"~{ROOT}:swarm:*:codex-viewers",
                    f"~{ROOT}:swarm:*:identity-conflicts",
                    f"~{ROOT}:swarm:*:desired-routes",
                    f"~{ROOT}:swarm:*:state-since",
                    f"~{ROOT}:swarm:*:claim:*",
                    f"~{ROOT}:swarm:*:ready",
                    f"~{ROOT}:swarm:*:restart-causes",
                    f"~{ROOT}:swarm:*:transfers",
                    f"~{ROOT}:swarm:*:handoff:*",
                    f"~{ROOT}:swarm:*:handoff-seat:*",
                    f"~{ROOT}:swarm:*:handoff-envelope:*",
                    f"~{ROOT}:swarm:*:findings",
                    f"~{ROOT}:swarm:*:checks:*",
                    f"~{ROOT}:swarm:*:merge-stall",
                    f"~{ROOT}:swarm:*:merge-stall-notice",
                    f"~{ROOT}:swarm:*:hive-placement-waits",
                    f"~{ROOT}:swarm:*:dev-red",
                    f"~{ROOT}:swarm:*:prerequisite-waits",
                    f"~{ROOT}:swarm:*:stage-covers",
                    f"~{ROOT}:seat:*:recaps",
                    f"~{ROOT}:seat:*:learned",
                    f"~{ROOT}:seat-of:*:exit",
                ),
                "channels": (f"{ROOT}:*",),
            },
        )
    ]


@pytest.mark.parametrize(
    ("suffix", "writable"),
    [
        ("hive:m1:inbox:item:abc", True),
        ("hive:m1:swarm:proof:heartbeat:engineer@0001-0001", True),
        ("hive:m2:inbox:item:abc", False),
        ("hive:m2:swarm:proof:heartbeat:engineer@0001-0001", False),
        ("hive:m10:inbox:item:abc", False),
        ("inbox:item:abc", False),
        ("inbox:owner:master@proof", False),
        ("swarm:proof:heartbeat:engineer@0001-0001", False),
        ("swarm:proof:stop-blocks:engineer@0001-0001", False),
    ],
)
def test_a_member_writes_its_own_inbox_and_readings_and_no_other_members(suffix, writable):
    from fnmatch import fnmatchcase

    key = f"{ROOT}:{suffix}"

    patterns = [pattern[1:] for pattern in auth._acl_keys("m1") if pattern.startswith("~")]

    assert any(fnmatchcase(key, pattern) for pattern in patterns) is writable
    assert any(fnmatchcase(key, pattern[3:]) for pattern in auth._acl_keys("m1") if pattern.startswith("%R~"))


@pytest.mark.parametrize(
    ("suffix", "writable"),
    [
        ("swarm:proof:claim:t1", True),
        ("swarm:proof:agents", True),
        ("swarm:proof:executions", True),
        ("swarm:proof:identity-conflicts", True),
        ("swarm:proof:desired-routes", True),
        ("swarm:proof:state-since", True),
        ("swarm:proof:ready", True),
        ("swarm:proof:restart-causes", True),
        ("swarm:proof:transfers", True),
        ("swarm:proof:handoff:t1", True),
        ("swarm:proof:handoff-seat:t1", True),
        ("swarm:proof:handoff-envelope:t1", True),
        ("swarm:proof:findings", True),
        ("swarm:proof:checks:https://github.com/o/r/pull/1", True),
        ("swarm:proof:checks:approval:https://github.com/o/r/pull/1", True),
        ("swarm:proof:checks:green:https://github.com/o/r/pull/1", True),
        ("swarm:proof:merge-stall", True),
        ("swarm:proof:merge-stall-notice", True),
        ("swarm:proof:hive-placement-waits", True),
        ("swarm:proof:dev-red", True),
        ("swarm:proof:prerequisite-waits", True),
        ("swarm:proof:stage-covers", True),
        ("swarm:proof:execution-publishing", True),
        ("swarm:proof:codex-transport", True),
        ("swarm:proof:codex-endpoints", True),
        ("swarm:proof:codex-openings", True),
        ("swarm:proof:codex-viewers", True),
        ("seat:eng-1@proof:recaps", True),
        ("seat:eng-1@proof:learned", True),
        ("seat-of:engineer@0001-0001:exit", True),
        ("swarm:proof:config", False),
        ("swarm:proof:claim-journal:t1", False),
        ("swarm:proof:task-authority:t1", False),
        ("swarm:proof:tick-lock", False),
        ("swarm:proof:launches", False),
        ("swarm:index", False),
        ("swarm:proof:dev-red-runs", False),
        ("seat:eng-1@proof:culture", False),
        ("names:engineer@0001-0001", False),
    ],
)
def test_a_member_writes_the_keys_a_worker_uses_for_done_ready_park_wait_and_status(suffix, writable):
    from fnmatch import fnmatchcase

    key = f"{ROOT}:{suffix}"
    patterns = [pattern[1:] for pattern in auth._acl_keys("m1") if pattern.startswith("~")]

    assert any(fnmatchcase(key, pattern) for pattern in patterns) is writable


def test_a_member_writes_the_keys_swarm_block_and_swarm_learned_write():
    from fnmatch import fnmatchcase

    from scripts.inbox.seats import SeatMemory
    from scripts.swarm import dev_red

    patterns = [pattern[1:] for pattern in auth._acl_keys("m1") if pattern.startswith("~")]

    for key in (dev_red.key("proof"), SeatMemory(None).key("eng-1@proof", "learned")):
        assert any(fnmatchcase(key, pattern) for pattern in patterns), key


def test_the_grant_sizes_and_member_record(admin, redis_lib):
    grant = auth.mint(admin, "laptop", PUBLIC)
    password = redis_lib.connection.parse_url(grant["redis_url"])["password"]

    assert len(grant["id"]) == 16
    int(grant["id"], 16)
    assert len(password) == 43
    assert len(grant["ledger_credential"]) == 43
    assert auth.member(admin, grant["id"]) == {
        "name": "laptop",
        "ledger": hashlib.sha256(grant["ledger_credential"].encode()).hexdigest(),
        "agents": {},
    }
    assert admin.acl_getuser(f"hive-{grant['id']}")["flags"] == ["on"]


def test_the_member_url_replaces_credentials_already_in_the_hive_url(admin):
    grant = auth.mint(admin, "laptop", "redis://admin:secret@hive.example:6380/2")

    assert "admin" not in grant["redis_url"]
    assert "secret" not in grant["redis_url"]
    assert grant["redis_url"].endswith("@hive.example:6380/2")


def test_a_refused_acl_user_leaves_no_member_records(admin, redis_lib, monkeypatch):
    def refuse(*args, **kwargs):
        raise redis_lib.exceptions.ResponseError("ERR Error in ACL SETUSER modifier")

    monkeypatch.setattr(admin, "acl_setuser", refuse)

    with pytest.raises(auth.HiveError) as refused:
        auth.mint(admin, "laptop", PUBLIC)

    assert str(refused.value) == "Redis refused the new member (ERR Error in ACL SETUSER modifier)"
    assert auth.members(admin) == {}
    assert admin.smembers(auth.INDEX) == set()
    assert admin.acl_users() == ["default"]


def _narrowed(admin, member_id):
    user = f"hive-{member_id}"
    minted = admin.acl_getuser(user)
    admin.acl_setuser(
        user, reset_keys=True, reset_channels=True, keys=["~stale"], channels=["stale"], categories=["-@all"]
    )
    assert f"~{ROOT}:seat:*:learned" not in admin.acl_getuser(user)["keys"]
    return user, minted


def test_regrant_gives_every_existing_member_the_current_rules_and_keeps_its_password(admin):
    narrowed = [_narrowed(admin, auth.mint(admin, name, PUBLIC)["id"]) for name in ("laptop", "desk")]

    auth.regrant(admin)

    for user, minted in narrowed:
        granted = admin.acl_getuser(user)
        assert f"~{ROOT}:seat:*:learned" in granted["keys"]
        for rule in ("keys", "channels", "categories", "passwords", "flags"):
            assert granted[rule] == minted[rule], rule


def test_serve_regrants_every_member_before_it_serves(admin, monkeypatch, capsys):
    user, minted = _narrowed(admin, auth.mint(admin, "laptop", PUBLIC)["id"])
    served = []

    class Endpoint:
        server_address = ("127.0.0.1", 8770)

        def serve_forever(self):
            served.append(admin.acl_getuser(user)["keys"])

    monkeypatch.setattr(cli, "redis_client", lambda: admin)
    monkeypatch.setattr(cli.ledger_ca, "served", lambda: None)
    monkeypatch.setattr(server, "make_server", lambda *args, **kwargs: Endpoint())

    assert cli.main(["serve", "--redis-url", PUBLIC]) == 0
    assert served == [minted["keys"]]


def test_revoke_cuts_both_credentials(admin):
    grant = auth.mint(admin, "laptop", PUBLIC)
    other = auth.mint(admin, "desk", PUBLIC)

    auth.revoke(admin, grant["id"])

    assert auth.ledger_member(admin, grant["ledger_credential"]) is None
    assert sorted(auth.members(admin)) == [other["id"]]
    assert sorted(admin.acl_users()) == ["default", f"hive-{other['id']}"]
    assert auth.ledger_member(admin, other["ledger_credential"]) == other["id"]
    assert admin.smembers(auth.INDEX) == {other["id"]}


def test_revoking_an_unknown_member_is_refused(admin):
    with pytest.raises(auth.HiveError, match="^no hive member nobody$"):
        auth.revoke(admin, "nobody")


def test_a_rediss_url_keeps_tls_and_its_query(admin):
    grant = auth.mint(admin, "laptop", "rediss://hive.example:6380/0?ssl_ca_certs=/ca.pem")

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


@pytest.mark.parametrize("bus_url", ["nats://bus.example.test:4222", "tls://other.example.test:4443"])
def test_joined_settings_select_the_delivered_nats_bus(tmp_path, bus_url):
    from dotenv import dotenv_values

    from scripts.events.config import BusConfig

    grant = {
        "id": "worker",
        "ledger_credential": "fixture",
        "redis_url": "redis://redis.example.test/0",
        "nats_credentials": "fixture bus credential",
        "bus_url": bus_url,
        "bus_scoped": True,
        "bus_swarms": ["rig"],
    }
    path = auth.write_env(tmp_path, "https://hive.example.test", grant)
    saved = dotenv_values(path)
    config = BusConfig.from_environ(saved)
    assert config.backend == "nats"
    assert config.url == bus_url
    assert saved["AGENTIHOOKS_BUS_BIND"] == "1"
    assert saved["AGENTIHOOKS_BUS_SCOPED"] == "1"
    assert saved["AGENTIHOOKS_BUS_SWARMS"] == "rig"
    assert saved["AGENTIHOOKS_BUS_CREDENTIALS"] == str(tmp_path / auth.NATS_CREDS_FILE)


def test_rejoining_tightens_an_existing_env_file(tmp_path):
    (tmp_path / "hive.env").write_text("old\n")
    (tmp_path / "hive.env").chmod(0o644)
    grant = {"id": "ab12", "name": "laptop", "ledger_credential": "led", "redis_url": "redis://u:p@h:1/0"}

    auth.write_env(tmp_path, "http://hive", grant)

    assert stat.S_IMODE((tmp_path / "hive.env").stat().st_mode) == 0o600
    assert "old" not in (tmp_path / "hive.env").read_text()


def test_a_new_env_file_is_created_private_in_a_new_home(tmp_path, monkeypatch):
    monkeypatch.setattr(auth.os, "fchmod", lambda fd, mode: None)
    previous = os.umask(0)
    try:
        path = auth.write_env(
            tmp_path / "a" / "b", "http://hive", {"id": "i", "ledger_credential": "l", "redis_url": "r"}
        )
    finally:
        os.umask(previous)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.fixture
def hive(admin):
    httpd = server.make_server(admin, PUBLIC, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.parametrize(
    ("path", "body", "status", "error"),
    [
        ("/other", b'{"ledger_credential": "x"}', 404, "not found"),
        ("/hive/join", b'{"code": "x"}', 404, "not found"),
        (server.REFRESH_PATH, b"not json", 400, server.BAD_REFRESH_BODY),
        (server.REFRESH_PATH, b"{}", 400, server.BAD_REFRESH_BODY),
        (server.REFRESH_PATH, b'{"ledger_credential": 1}', 400, server.BAD_REFRESH_BODY),
        (server.REFRESH_PATH, b'{"ledger_credential": null}', 400, server.BAD_REFRESH_BODY),
        (server.REFRESH_PATH, b"[]", 400, server.BAD_REFRESH_BODY),
        (
            server.REFRESH_PATH,
            b'{"ledger_credential": "' + b"x" * server.MAX_BODY + b'"}',
            400,
            server.BAD_REFRESH_BODY,
        ),
    ],
)
def test_the_hive_endpoint_refuses_join_and_other_paths_and_bad_bodies(hive, path, body, status, error):
    request = urllib.request.Request(hive + path, data=body, method="POST")

    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request, timeout=5)

    assert refused.value.code == status
    assert json.load(refused.value) == {"error": error}


def test_cli_revoke(admin, monkeypatch, capsys):
    monkeypatch.setattr(cli, "redis_client", lambda: admin)

    grant = auth.mint(admin, "laptop", PUBLIC)
    assert cli.main(["revoke", grant["id"]]) == 0
    assert capsys.readouterr().out == f"revoked {grant['id']}\n"
    assert cli.main(["revoke", grant["id"]]) == 1
    assert capsys.readouterr().err == f"hive revoke refused: no hive member {grant['id']}\n"


def test_a_hive_endpoint_off_loopback_needs_tls(admin):
    with pytest.raises(
        auth.HiveError,
        match="^a hive endpoint off loopback hands out credentials, so it needs --tls-cert and --tls-key$",
    ):
        server.make_server(admin, PUBLIC, "0.0.0.0", 0)


@pytest.mark.parametrize(
    ("host", "loopback"), [("127.0.0.1", True), ("localhost", True), ("0.0.0.0", False), ("", False)]
)
def test_is_loopback(host, loopback):
    assert server.is_loopback(host) is loopback


@pytest.fixture
def cert(tmp_path):
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
            "-keyout",
            tmp_path / "key.pem",
            "-out",
            tmp_path / "cert.pem",
        ],
        check=True,
        capture_output=True,
    )
    return str(tmp_path / "cert.pem"), str(tmp_path / "key.pem")


def test_a_stalled_tls_client_does_not_block_an_https_refresh(admin, cert, tmp_path, monkeypatch, capsys):
    httpd = server.make_server(admin, PUBLIC, "localhost", 0, cert)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    stalled = socket.create_connection(("localhost", httpd.server_address[1]))
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "home"))
    trusting = ssl.create_default_context(cafile=cert[0])
    real = urllib.request.urlopen
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda request, timeout: real(request, timeout=timeout, context=trusting)
    )
    monkeypatch.setenv("AGENTIHOOKS_HIVE_URL", f"https://localhost:{httpd.server_address[1]}")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL", auth.mint(admin, "laptop", PUBLIC)["ledger_credential"])
    try:
        assert cli.main(["refresh"]) == 0
    finally:
        stalled.close()
        httpd.shutdown()
        httpd.server_close()

    assert capsys.readouterr().out.startswith("refreshed the Redis credential of hive member ")
    assert (tmp_path / "home" / "hive.env").exists()


def test_an_unreadable_tls_certificate_is_refused(admin):
    with pytest.raises(auth.HiveError, match=r"^the TLS certificate or key cannot be loaded \("):
        server.make_server(admin, PUBLIC, "127.0.0.1", 0, ("/missing.pem", "/missing.pem"))


def test_refresh_reports_a_body_that_is_not_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIHOOKS_HIVE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_LEDGER_CREDENTIAL", "led")
    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: io.BytesIO(b"<html>"))

    assert cli.main(["refresh"]) == 1

    assert (
        capsys.readouterr().err
        == "hive refresh refused: the hive at http://127.0.0.1:1 answered with a body that is not JSON\n"
    )


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


def test_redis_client_connects_over_tls_for_a_rediss_url(redis_lib, monkeypatch):
    monkeypatch.setattr(redis_lib.Redis, "ping", lambda self: True)

    client = store.redis_client(
        {"AGENTIHOOKS_DEPLOYMENT": "distributed", "AGENTIHOOKS_HIVE_REDIS_URL": "rediss://hive-a:p@h:1/0"}
    )

    assert client.connection_pool.connection_class is redis_lib.connection.SSLConnection
    assert client.connection_pool.connection_kwargs["username"] == "hive-a"


def test_a_member_credential_runs_a_checked_wait_and_a_cover_and_no_other_writes(monkeypatch):
    import fakeredis
    from redis.exceptions import NoPermissionError

    from scripts.swarm import idle, prerequisite_wait, stage_cover

    server = fakeredis.FakeServer()
    admin = fakeredis.FakeRedis(server=server, decode_responses=True)
    admin.acl_setuser(
        "hive-m1",
        enabled=True,
        nopass=True,
        categories=auth.ACL_CATEGORIES,
        keys=auth._acl_keys("m1"),
        channels=auth.ACL_PATTERNS,
    )
    reads = {"GET", "HGET", "HGETALL", "HEXISTS", "EXISTS", "SMEMBERS", "SISMEMBER", "MGET", "HKEYS", "TTL", "PTTL"}

    # fakeredis drops %R~ read selectors, so reads the member's %R~ grant allows go through the admin client.
    class Member(fakeredis.FakeRedis):
        def execute_command(self, *args, **options):
            if str(args[0]).upper() in reads:
                return admin.execute_command(*args, **options)
            return super().execute_command(*args, **options)

    member = Member(server=server, decode_responses=True, username="hive-m1")
    member.auth("", username="hive-m1")
    assert member.acl_whoami() == "hive-m1"
    assert f"%R~{ROOT}:*" in auth._acl_keys("m1")
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "distributed")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_ID", "m1")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_REDIS_URL", "redis://hive.example:6380/2")
    swarm = store.RedisStore(member)
    agent = store.AgentRecord(name="engineer@0001-0001", lane="eng", task="t1")
    held = {"kind": "checks", "target": "https://github.com/o/r/pull/1"}
    admin.hset(prerequisite_wait.key("proof"), "t1", "{}")

    prerequisite_wait.admit(swarm, "proof", agent, held, None, 1_000, False)
    idle.declare_wait(member, "proof", agent.name, 61_000, "checks", 1_000, on=held)
    covered = stage_cover.cover(
        swarm, "proof", {"phases": [{"id": "p1"}]}, stage_cover.Request("build", "phases/p1", "fix", "d"), 1_000
    )

    assert not admin.hexists(prerequisite_wait.key("proof"), "t1")
    assert idle.wait(member, "proof", agent.name)["on"] == held
    assert admin.exists(f"{ROOT}:hive:m1:swarm:proof:wait:{agent.name}")
    assert json.loads(admin.hget(swarm.key("proof", stage_cover.KEY), "build")) == covered
    for forbidden in (f"{ROOT}:hive:m2:swarm:proof:wait:{agent.name}", swarm.key("proof", "config")):
        with pytest.raises(NoPermissionError):
            member.set(forbidden, "write")
