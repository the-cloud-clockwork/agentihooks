import json
import socket
import subprocess
import threading
from types import SimpleNamespace

import pytest

from scripts import openbao_kv
from scripts.events import nkey
from scripts.events.config import BusConfig
from scripts.hive import auth, nats_grant, remove, server
from tests.events import nats_server

pytestmark = pytest.mark.unit

PUBLIC = "redis://hive.example:6380/2"
SYSTEM_LIMITS = {"subs": -1, "data": -1, "payload": -1, "imports": -1, "exports": -1, "conn": -1, "leaf": -1}


@pytest.fixture
def admin():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


def _user_creds(account: nkey.KeyPair, name: str) -> str:
    user = nkey.create(nkey.USER)
    return nkey.creds(nkey.encode_jwt(account, user.public, name, nats_grant.user_claims()), user.seed)


@pytest.fixture
def signer(tmp_path):
    operator, system, account = nkey.create(nkey.OPERATOR), nkey.create(nkey.ACCOUNT), nkey.create(nkey.ACCOUNT)
    port = nats_server.free_port()
    signed = nats_grant.Signer(operator, account, _user_creds(system, "sys"), f"nats://127.0.0.1:{port}")
    claims = {"type": "operator", "version": 2, "system_account": system.public}
    system_jwt = nkey.encode_jwt(
        operator, system.public, "SYS", {"type": "account", "version": 2, "limits": SYSTEM_LIMITS}
    )
    config = tmp_path / "nats.conf"
    config.write_text(
        f"host: 127.0.0.1\nport: {port}\n"
        f"operator: {nkey.encode_jwt(operator, operator.public, 'op', claims)}\n"
        f"system_account: {system.public}\n"
        f'jetstream: {{store_dir: "{tmp_path / "js"}"}}\n'
        f'resolver: {{type: full, dir: "{tmp_path / "jwt"}"}}\n'
        f"resolver_preload: {{\n  {system.public}: {system_jwt}\n"
        f"  {account.public}: {nats_grant.account_jwt(signed, {})}\n}}\n"
    )
    process = subprocess.Popen(
        [nats_server.binary(), "-c", str(config)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        nats_server.wait_listening(port, process)
        yield signed
    finally:
        process.terminate()
        process.wait(10)


def _opens(signer: nats_grant.Signer, credentials: str | None) -> bool:
    from scripts.events import nats

    try:
        bus = nats.open_bus(BusConfig(backend="nats", url=signer.url, credentials=credentials))
    except Exception as exc:
        assert "authorization" in str(exc).lower()
        return False
    bus.close()
    return True


def _refresh(admin, signer, ledger_credential: str) -> dict:
    httpd = server.make_server(admin, PUBLIC, "127.0.0.1", 0, signer=signer)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    payload = json.dumps({"ledger_credential": ledger_credential}).encode()
    try:
        with socket.create_connection(("127.0.0.1", httpd.server_address[1]), timeout=5) as conn:
            conn.sendall(b"POST /hive/refresh HTTP/1.0\r\nContent-Length: %d\r\n\r\n%s" % (len(payload), payload))
            reply = b""
            while chunk := conn.recv(65536):
                reply += chunk
    finally:
        httpd.shutdown()
        httpd.server_close()
    return json.loads(reply.partition(b"\r\n\r\n")[2])


def test_the_server_refuses_a_bus_with_no_signed_credential(signer):
    assert not _opens(signer, None)


def test_hive_serve_mints_a_member_credential_the_bus_opens_with(admin, signer):
    member = auth.mint(admin, "h1", PUBLIC)
    grant = _refresh(admin, signer, member["ledger_credential"])
    jwt, user = nkey.parse_creds(grant["nats_credentials"])
    assert user == nats_grant.member_user(signer, member["id"])
    assert _opens(signer, grant["nats_credentials"])


def _violations(signer: nats_grant.Signer, credentials: str, act, refused: bool = False) -> list[str]:
    import asyncio

    import nats as nats_client

    from scripts.events.nats import auth_options

    async def run() -> list[str]:
        seen, violated = [], asyncio.Event()

        async def error_cb(exc):
            seen.append(str(exc))
            violated.set()

        nc = await nats_client.connect(
            signer.url, allow_reconnect=False, error_cb=error_cb, **auth_options(credentials)
        )
        try:
            await act(nc)
            await nc.flush()
            if refused:
                # nats-py writes the flush PING ahead of buffered commands, so the -ERR can follow its PONG.
                await asyncio.wait_for(violated.wait(), 5)
        finally:
            await nc.close()
        return seen

    return asyncio.run(run())


def test_a_member_credential_cannot_publish_outside_the_swarm_prefix(signer):
    async def act(nc):
        await nc.publish("outside.events", b"x")

    assert _violations(signer, nats_grant.mint(signer, "m1"), act, refused=True) == [
        'nats: permissions violation for publish to "outside.events"'
    ]


def test_a_member_credential_cannot_subscribe_outside_the_swarm_prefix(signer):
    async def act(nc):
        await nc.subscribe("outside.>")

    assert _violations(signer, nats_grant.mint(signer, "m1"), act, refused=True) == [
        'nats: permissions violation for subscription to "outside.>"'
    ]


def test_a_member_credential_publishes_and_subscribes_under_the_swarm_prefix(signer):
    received = []

    async def act(nc):
        subscription = await nc.subscribe("swarm.s1.probe")
        await nc.publish("swarm.s1.probe", b"x")
        received.append((await subscription.next_msg(timeout=5)).data)

    assert _violations(signer, nats_grant.mint(signer, "m1"), act) == []
    assert received == [b"x"]


def test_the_bus_logs_queues_dead_letters_and_keeps_state_under_a_member_credential(signer):
    from scripts.events import nats
    from scripts.events.config import QueuePolicy
    from scripts.events.envelope import Envelope
    from scripts.events.subjects import subject

    policy = QueuePolicy(max_deliver=3, ack_wait_ms=1_000)
    event = Envelope(
        id="e1", type="task.claimed", swarm="s1", entity="task", key="t1", version=1, time=0, author="a", data={}
    )
    bus = nats.open_bus(
        BusConfig(backend="nats", url=signer.url, credentials=nats_grant.mint(signer, "m1"), queue=policy)
    )
    try:
        assert bus.events.publish(subject("s1", "task", "claimed"), event)
        [logged] = bus.events.consumer("member", "swarm.s1.>").fetch(1, 5_000)
        logged.ack()
        queue = bus.queue("member", policy)
        assert queue.offer(event)
        taken = queue.take(5_000)
        taken.ack()
        assert bus.dead.publish("dead.s1", event)
        state = bus.state("member")
        revision = state.create("k", b"v")
        assert (logged.event, taken.event, state.get("k").revision) == (event, event, revision)
    finally:
        bus.close()


def test_a_hive_endpoint_without_a_signer_mints_no_bus_credential(admin):
    member = auth.mint(admin, "h1", PUBLIC)
    grant = _refresh(admin, None, member["ledger_credential"])
    assert grant["id"] == member["id"]
    assert "nats_credentials" not in grant


def test_removing_a_member_revokes_its_bus_credential(admin, signer):
    member = auth.mint(admin, "h1", PUBLIC)
    other = auth.mint(admin, "h2", PUBLIC)
    creds = _refresh(admin, signer, member["ledger_credential"])["nats_credentials"]
    kept = _refresh(admin, signer, other["ledger_credential"])["nats_credentials"]
    lines = remove.revoke(admin, member["id"], signer)
    assert lines == [
        f"revoked the NATS credential of {member['id']}",
        f"revoked member {member['id']} and its Redis user",
    ]
    assert not _opens(signer, creds)
    assert _opens(signer, kept)
    assert set(admin.hgetall(nats_grant.REVOKED)) == {nats_grant.member_user(signer, member["id"]).public}


def test_a_claims_update_the_server_refuses_raises(signer):
    with pytest.raises(auth.HiveError, match=r"^NATS refused the account update \(jwt update resulted in error"):
        nats_grant.push(signer, "not.a.jwt")


def test_a_failed_push_keeps_the_revocation_for_the_next_push(admin, signer):
    unreachable = nats_grant.Signer(
        signer.operator, signer.account, signer.system_credentials, f"nats://127.0.0.1:{nats_server.free_port()}"
    )
    with pytest.raises(Exception):
        nats_grant.revoke(admin, unreachable, "m1")
    assert set(admin.hgetall(nats_grant.REVOKED)) == {nats_grant.member_user(signer, "m1").public}


def test_a_role_credential_named_in_openbao_opens_the_bus(signer, monkeypatch):
    role = _user_creds(signer.account, "ledger")
    reads = []
    monkeypatch.setattr(openbao_kv, "read", lambda bao, path: (reads.append(path), ({"credentials": role}, 1))[1])
    environ = {
        "AGENTIHOOKS_BUS": "nats",
        "AGENTIHOOKS_BUS_URL": signer.url,
        "AGENTIHOOKS_BUS_CREDENTIALS": "openbao:k8s/agentihooks-swarm-nats-ledger",
        "OPENBAO_URL": "https://bao.example",
        "OPENBAO_TOKEN": "t",
    }
    config = BusConfig.from_environ(environ)
    assert reads == ["k8s/agentihooks-swarm-nats-ledger"]
    assert config.credentials == role
    assert _opens(signer, config.credentials)


def test_the_signer_comes_from_the_openbao_record_named_by_reference(monkeypatch):
    operator, account = nkey.create(nkey.OPERATOR), nkey.create(nkey.ACCOUNT)
    record = {"operator_seed": operator.seed, "account_seed": account.seed, "system_credentials": "c", "url": "u"}
    expected = (openbao_kv.OpenBao("https://bao.example", "t"), "k8s/nats-signer")
    monkeypatch.setattr(openbao_kv, "read", lambda bao, path: (record, 1) if (bao, path) == expected else None)
    environ = {nats_grant.SIGNER_ENV: "k8s/nats-signer", "OPENBAO_URL": "https://bao.example", "OPENBAO_TOKEN": "t"}
    assert nats_grant.from_environ(environ) == nats_grant.Signer(operator, account, "c", "u")
    assert nats_grant.from_environ({}) is None


def test_a_signer_record_missing_a_field_is_refused(monkeypatch):
    monkeypatch.setattr(openbao_kv, "read", lambda bao, path: ({"operator_seed": "x"}, 1))
    environ = {nats_grant.SIGNER_ENV: "k8s/nats-signer", "OPENBAO_URL": "https://bao.example", "OPENBAO_TOKEN": "t"}
    with pytest.raises(auth.HiveError, match="^the NATS signer record k8s/nats-signer cannot be used"):
        nats_grant.from_environ(environ)


def test_a_signer_reference_needs_openbao_settings():
    with pytest.raises(auth.HiveError, match="names an OpenBao record"):
        nats_grant.from_environ({nats_grant.SIGNER_ENV: "k8s/nats-signer"})


def test_refresh_writes_the_bus_credential_beside_the_member_env(tmp_path):
    grant = {"id": "m1", "ledger_credential": "l", "redis_url": "r", "nats_credentials": "creds text"}
    env = auth.write_env(tmp_path, "https://hive", grant).read_text()
    creds = tmp_path / auth.NATS_CREDS_FILE
    assert creds.read_text() == "creds text"
    assert oct(creds.stat().st_mode & 0o777) == "0o600"
    assert env.endswith(f"AGENTIHOOKS_BUS_CREDENTIALS={creds}\n")
    env = auth.write_env(tmp_path, "https://hive", {**grant, "nats_credentials": None}).read_text()
    assert "AGENTIHOOKS_BUS_CREDENTIALS" not in env
    assert not creds.exists()


@pytest.fixture
def wired(admin, monkeypatch):
    from scripts.hive import cli

    signed, revoked = object(), []
    monkeypatch.setattr(cli, "redis_client", lambda: admin)
    monkeypatch.setattr(nats_grant, "from_environ", lambda environ: signed)
    monkeypatch.setattr(
        nats_grant, "revoke", lambda redis, signer, member: (revoked.append((redis, signer, member)), "bus")[1]
    )
    return cli, signed, revoked


def test_hive_revoke_revokes_the_bus_credential_then_the_member(admin, wired, capsys):
    cli, signed, revoked = wired
    member = auth.mint(admin, "h1", PUBLIC)
    assert cli.main(["revoke", member["id"]]) == 0
    assert capsys.readouterr().out == f"bus\nrevoked {member['id']}\n"
    assert revoked == [(admin, signed, member["id"])]
    assert not admin.exists(remove.member_key(member["id"]))


def test_hive_remove_and_serve_carry_the_signer(admin, wired, monkeypatch):
    cli, signed, _ = wired
    plans, served = [], []
    monkeypatch.setenv(openbao_kv.URL_ENV, "https://bao.example")
    monkeypatch.setenv(openbao_kv.TOKEN_ENV, "t")
    monkeypatch.setattr(remove, "remove", lambda store, bao, plan, host: (plans.append(plan), [])[1])
    assert cli.main(["remove", auth.mint(admin, "h1", PUBLIC)["id"]]) == 0
    assert [plan.signer for plan in plans] == [signed]

    def make_server(*args, **kwargs):
        served.append(kwargs["signer"])
        return SimpleNamespace(server_address=("127.0.0.1", 1), serve_forever=lambda: None)

    monkeypatch.setattr(server, "make_server", make_server)
    monkeypatch.setenv("AGENTIHOOKS_SWARM_REDIS_URL", "redis://own:6379/0")
    assert cli.main(["serve"]) == 0
    assert served == [signed]


def test_remove_revokes_with_the_plan_signer(admin, monkeypatch):
    signed, calls = object(), []
    for step in ("drain", "forget", "unbank", "unmanifest"):
        monkeypatch.setattr(remove, step, lambda *args: [])
    monkeypatch.setattr(remove.machine, "forget", lambda *args: [])
    monkeypatch.setattr(remove, "revoke", lambda *args: (calls.append(args), ["revoked"])[1])
    store = SimpleNamespace(redis=admin)
    plan = remove.Plan("m1", "h1", "k8s/agentihooks-hive-h1", signer=signed)
    assert remove.remove(store, None, plan, remove.Host()) == ["revoked"]
    assert calls == [(admin, "m1", signed)]


def _claims(jwt: str) -> dict:
    import base64

    segment = jwt.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))


def _offline_signer() -> nats_grant.Signer:
    return nats_grant.Signer(nkey.create(nkey.OPERATOR), nkey.create(nkey.ACCOUNT), "creds", "nats://hub:4222")


def test_a_member_credential_carries_exactly_the_user_claims():
    signed = _offline_signer()
    jwt, user = nkey.parse_creds(nats_grant.mint(signed, "m1"))
    claims = _claims(jwt)
    assert (claims["iss"], claims["sub"], claims["name"]) == (signed.account.public, user.public, "hive-m1")
    assert claims["nats"] == {
        "type": "user",
        "version": 2,
        "pub": {"allow": ["swarm.>", "dead.>", "agentihooks.queue.>", "$KV.>", "$JS.API.>", "$JS.ACK.>"]},
        "sub": {"allow": ["swarm.>", "_INBOX.>"]},
        "subs": -1,
        "data": -1,
        "payload": -1,
    }


@pytest.mark.parametrize("revocations", [{}, {"UA": 7}])
def test_the_account_jwt_carries_exactly_the_account_claims(revocations):
    signed = _offline_signer()
    claims = _claims(nats_grant.account_jwt(signed, revocations))
    assert (claims["iss"], claims["sub"], claims["name"]) == (signed.operator.public, signed.account.public, "SWARM")
    assert claims["nats"] == {
        "type": "account",
        "version": 2,
        "limits": nats_grant.ACCOUNT_LIMITS,
        "default_permissions": {"pub": {}, "sub": {}},
        **({"revocations": revocations} if revocations else {}),
    }
    assert nats_grant.ACCOUNT_LIMITS["disk_storage"] == nats_grant.ACCOUNT_LIMITS["mem_storage"] == -1


def test_a_push_connects_once_without_reconnecting_and_bounds_both_waits(monkeypatch):
    import nats as nats_client

    signed = nats_grant.Signer(
        nkey.create(nkey.OPERATOR), nkey.create(nkey.ACCOUNT), _user_creds(nkey.create(nkey.ACCOUNT), "sys"), "nats://x"
    )
    seen = {}

    class Client:
        async def request(self, subject, payload, **kwargs):
            seen["request"] = (subject, payload, kwargs)
            return SimpleNamespace(data=b'{"data": {"code": 200}}')

        async def close(self):
            seen["closed"] = True

    async def connect(url, **kwargs):
        seen["connect"] = (url, {key: kwargs[key] for key in ("allow_reconnect", "connect_timeout")}, sorted(kwargs))
        return Client()

    monkeypatch.setattr(nats_client, "connect", connect)
    nats_grant.push(signed, "a.b.c")
    assert seen == {
        "connect": (
            "nats://x",
            {"allow_reconnect": False, "connect_timeout": 10},
            ["allow_reconnect", "connect_timeout", "signature_cb", "user_jwt_cb"],
        ),
        "request": ("$SYS.REQ.CLAIMS.UPDATE", b"a.b.c", {"timeout": 10}),
        "closed": True,
    }
