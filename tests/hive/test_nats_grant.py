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
def signer(tmp_path, request):
    operator, system, account = nkey.create(nkey.OPERATOR), nkey.create(nkey.ACCOUNT), nkey.create(nkey.ACCOUNT)
    port = nats_server.free_port()
    signed = nats_grant.Signer(
        operator, account, _user_creds(system, "sys"), f"nats://127.0.0.1:{port}", operator.public
    )
    claims = {"type": "operator", "version": 2, "system_account": system.public}
    system_jwt = nkey.encode_jwt(
        operator, system.public, "SYS", {"type": "account", "version": 2, "limits": SYSTEM_LIMITS}
    )
    config = tmp_path / "nats.conf"
    config.write_text(
        f"host: 127.0.0.1\nport: {port}\nmax_control_line: {getattr(request, 'param', 65536)}\n"
        f"operator: {nkey.encode_jwt(operator, operator.public, 'op', claims)}\n"
        f"system_account: {system.public}\n"
        f'jetstream: {{store_dir: "{tmp_path / "js"}"}}\n'
        f'resolver: {{type: full, dir: "{tmp_path / "jwt"}"}}\n'
        f"resolver_preload: {{\n  {system.public}: {system_jwt}\n"
        f"  {account.public}: {nats_grant.account_jwt(operator, account, {})}\n}}\n"
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
        bus = nats.open_bus(BusConfig(backend="nats", url=signer.url, credentials=credentials, provision=False))
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


def test_a_member_credential_cannot_read_the_replies_of_other_members(signer):
    async def act(nc):
        await nc.subscribe("_INBOX.>")

    assert _violations(signer, nats_grant.mint(signer, "m1"), act, refused=True) == [
        'nats: permissions violation for subscription to "_inbox.>"'
    ]


@pytest.mark.parametrize("verb", ["DELETE", "PURGE", "UPDATE"])
def test_a_member_credential_cannot_change_a_stream(signer, verb):
    subject = f"$JS.API.STREAM.{verb}.AGENTIHOOKS_EVENTS"

    async def act(nc):
        await nc.publish(subject, b"{}")

    assert _violations(signer, nats_grant.mint(signer, "m1"), act, refused=True) == [
        f'nats: permissions violation for publish to "{subject.lower()}"'
    ]


def _refused(signer: nats_grant.Signer, credentials: str) -> list[str]:
    import asyncio

    import nats as nats_client

    from scripts.events import nats

    errors = []

    async def attempt() -> None:
        async def collect(exc):
            errors.append(str(exc).lower())

        nc = await nats_client.connect(
            signer.url, allow_reconnect=False, error_cb=collect, **nats.auth_options(credentials)
        )
        js = nc.jetstream(timeout=1)
        try:
            for create in (
                js.add_stream(name="ROGUE", subjects=["rogue.>"]),
                js.add_consumer(nats.EVENT_STREAM, durable_name="rogue"),
            ):
                with pytest.raises(nats_client.errors.TimeoutError):
                    await create
            for _ in range(100):
                if len(errors) >= 2:
                    break
                await asyncio.sleep(0.05)
        finally:
            await nc.close()

    asyncio.run(attempt())
    return errors


def test_a_member_takes_hub_offered_work_but_is_refused_creating_a_stream_or_durable(admin, signer):
    from scripts.events import nats
    from scripts.events.config import QueuePolicy
    from scripts.events.envelope import Envelope
    from scripts.hive import buckets

    hub = nats.open_bus(BusConfig(backend="nats", url=signer.url, credentials=_user_creds(signer.account, "hub")))
    member = nats.open_bus(
        BusConfig(backend="nats", url=signer.url, credentials=nats_grant.mint(signer, "m1"), provision=False)
    )
    try:
        policy = QueuePolicy()
        with pytest.raises(nats.NotFoundError):
            member.queue("swarm.rig.ready.eng", policy)
        buckets.provision(hub)
        event = Envelope("offer-1", "task.ready", "rig", "task", "t1", 0, 1, "readiness", {"lane": "eng"})
        hub.queue("swarm.rig.ready.eng", policy).offer(event)
        delivery = member.queue("swarm.rig.ready.eng", policy).take(1_000)
        assert delivery.event.id == "offer-1"
        delivery.ack()
        for bucket in buckets.NAMES:
            store = member.state(bucket)
            assert store.create("k", b"v") == 1
            assert store.update("k", b"w", 1) == 2
            assert store.get("k").value == b"w"
            assert [entry.value for entry in store.watch("k")] == [b"v", b"w"]
        assert member.events.publish("swarm.rig.execution.started", event)
        assert member.dead.publish("dead.rig", event)
    finally:
        member.close()
        hub.close()
    refused = " ".join(_refused(signer, nats_grant.mint(signer, "m1")))
    assert 'permissions violation for publish to "$js.api.stream.create.rogue"' in refused
    durable = f'"$js.api.consumer.durable.create.{nats.EVENT_STREAM}.rogue"'.lower()
    assert f"permissions violation for publish to {durable}" in refused


def test_hive_members_share_account_capacity_through_the_hub_slot_service(admin, signer):
    from scripts.events import nats
    from scripts.hive import account_slots, buckets
    from scripts.hub import slots

    hub = nats.open_bus(BusConfig(backend="nats", url=signer.url, credentials=_user_creds(signer.account, "hub")))
    members = [
        nats.open_bus(
            BusConfig(backend="nats", url=signer.url, credentials=nats_grant.mint(signer, name), provision=False)
        )
        for name in ("m1", "m2")
    ]
    try:
        buckets.provision(hub)
        slots.serve(hub, clock=lambda: 1000)
        report = {"slots": [{"harness": "claude", "account": "a", "cap": 1}], "sessions": {"claude:a": 0}}
        first = account_slots.Remote(account_slots.asker(members[0], "rig"), "m1", lambda: report)
        second = account_slots.Remote(account_slots.asker(members[1], "other"), "m2", lambda: report)
        assert first.take("one", "claude") == "a"
        assert second.take("two", "claude") is None
        first.release("one", "claude", "a")
        assert second.take("two", "claude") == "a"
    finally:
        for member in members:
            member.close()
        hub.close()


@pytest.mark.parametrize("bucket", ["account-slots", "hive-slot-accounts"])
def test_a_member_credential_cannot_read_the_hub_owned_account_buckets(signer, bucket):
    async def act(nc):
        await nc.publish(f"$JS.API.STREAM.INFO.KV_{bucket}", b"")
        await nc.publish(f"$KV.{bucket}.key", b"")

    seen = " ".join(_violations(signer, nats_grant.mint(signer, "m1"), act, refused=True)).lower()
    assert f'permissions violation for publish to "$js.api.stream.info.kv_{bucket}"' in seen


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
        signer.operator,
        signer.account,
        signer.system_credentials,
        f"nats://127.0.0.1:{nats_server.free_port()}",
        signer.trusted_operator,
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


def test_revoke_refuses_an_account_update_from_an_untrusted_operator(admin, signer):
    member = auth.mint(admin, "h1", PUBLIC)
    creds = _refresh(admin, signer, member["ledger_credential"])["nats_credentials"]
    stranger = nkey.create(nkey.OPERATOR)
    untrusted = nats_grant.Signer(
        stranger, signer.account, signer.system_credentials, signer.url, signer.trusted_operator
    )
    message = (
        f"refusing to push an account update issued by {stranger.public}, "
        f"which is not the trusted operator {signer.trusted_operator}"
    )
    with pytest.raises(auth.HiveError) as refused:
        nats_grant.revoke(admin, untrusted, member["id"])
    assert str(refused.value) == message
    assert _opens(signer, creds)
    assert _opens(signer, _refresh(admin, signer, member["ledger_credential"])["nats_credentials"])


def test_the_trusted_operator_signs_the_revoking_account_update(admin, signer, monkeypatch):
    pushed = []
    monkeypatch.setattr(nats_grant, "push", lambda signed, jwt: pushed.append((signed, jwt)))
    nats_grant.revoke(admin, signer, "m1")
    [(signed, jwt)] = pushed
    claims = nkey.claims(jwt)
    assert signed is signer
    assert (claims["iss"], claims["sub"]) == (signer.trusted_operator, signer.account.public)


def test_claims_reads_the_payload_of_a_jwt():
    operator = nkey.create(nkey.OPERATOR)
    claims = nkey.claims(nkey.encode_jwt(operator, "subject", "n", {"type": "account"}))
    assert (claims["iss"], claims["sub"], claims["name"], claims["nats"]) == (
        operator.public,
        "subject",
        "n",
        {"type": "account"},
    )


@pytest.mark.parametrize("payload", [b'{"a":1}', b'{"ab":1}', b'{"abc":1}'])
def test_claims_reads_a_payload_of_every_padding_length(payload):
    assert nkey.claims(f"a.{nkey.b64(payload)}.c") == json.loads(payload)


@pytest.mark.parametrize(
    "jwt",
    [
        "",
        "one",
        "a.!!!.c",
        f"a.{nkey.b64(b'{}')}.c.d",
        f"a.{nkey.b64(b'{}')}",
        f"a.{nkey.b64(b'[1]')}.c",
        f"a.{nkey.b64(b'not json')}.c",
    ],
)
def test_claims_refuses_a_malformed_jwt(jwt):
    with pytest.raises(nkey.NkeyError, match="^not a JWT with a claims object$"):
        nkey.claims(jwt)


def _signer_record(operator, account) -> dict:
    return {"operator_seed": operator.seed, "account_seed": account.seed, "system_credentials": "c", "url": "u"}


SIGNER_PATH = "agentihooks/hub/nats-signer"
TRUSTED = nkey.create(nkey.OPERATOR).public
BAO_ENV = {"OPENBAO_URL": "https://bao.example", "OPENBAO_TOKEN": "t", nats_grant.TRUSTED_OPERATOR_ENV: TRUSTED}


def test_the_signer_comes_from_the_openbao_record_named_by_reference(monkeypatch):
    operator, account = nkey.create(nkey.OPERATOR), nkey.create(nkey.ACCOUNT)
    record = _signer_record(operator, account)
    expected = (openbao_kv.OpenBao("https://bao.example", "t"), SIGNER_PATH)
    monkeypatch.setattr(openbao_kv, "read", lambda bao, path: (record, 1) if (bao, path) == expected else None)
    environ = {nats_grant.SIGNER_ENV: SIGNER_PATH, **BAO_ENV}
    assert nats_grant.from_environ(environ) == nats_grant.Signer(operator, account, "c", "u", TRUSTED)
    assert nats_grant.from_environ({}) is None


def test_the_trusted_operator_never_comes_from_the_signer_record(monkeypatch):
    operator, account = nkey.create(nkey.OPERATOR), nkey.create(nkey.ACCOUNT)
    record = {**_signer_record(operator, account), "trusted_operator": operator.public}
    monkeypatch.setattr(openbao_kv, "read", lambda bao, path: (record, 1))
    assert nats_grant.from_environ({nats_grant.SIGNER_ENV: SIGNER_PATH, **BAO_ENV}).trusted_operator == TRUSTED


@pytest.mark.parametrize("trusted", [None, ""])
def test_a_signer_needs_the_trusted_operator_from_the_environment(monkeypatch, trusted):
    reads = []
    monkeypatch.setattr(openbao_kv, "read", lambda bao, found: reads.append(found))
    environ = {nats_grant.SIGNER_ENV: SIGNER_PATH, **BAO_ENV, nats_grant.TRUSTED_OPERATOR_ENV: trusted}
    if trusted is None:
        del environ[nats_grant.TRUSTED_OPERATOR_ENV]
    with pytest.raises(auth.HiveError) as refused:
        nats_grant.from_environ(environ)
    assert str(refused.value) == (
        "AGENTIHOOKS_NATS_SIGNER is set, so AGENTIHOOKS_NATS_TRUSTED_OPERATOR must name "
        "the operator public key the NATS server trusts"
    )
    assert reads == []


@pytest.mark.parametrize(
    "path",
    [
        "k8s/nats-signer",
        "agentihooks/nats-signer",
        "agentihooks/hub",
        "agentihooks/hub/",
        "x/agentihooks/hub/s",
        "agentihooks/hub/../../k8s/x",
        "agentihooks/hub/a/../b",
        "agentihooks/hub/./s",
        "agentihooks/hub//s",
        "agentihooks/hub/s/",
        "agentihooks/hub/%2e%2e/k8s/x",
        "agentihooks/hub/%2e%2e%2fk8s%2fx",
    ],
)
def test_a_signer_record_outside_the_hub_path_is_refused(monkeypatch, path):
    reads = []
    monkeypatch.setattr(openbao_kv, "read", lambda bao, found: reads.append(found))
    with pytest.raises(auth.HiveError) as refused:
        nats_grant.from_environ({nats_grant.SIGNER_ENV: path, **BAO_ENV})
    assert str(refused.value) == (
        f"the NATS signer record {path} holds the system account credential, "
        "so it must live under the OpenBao path agentihooks/hub/ that only the hub role reads"
    )
    assert reads == []


def test_a_nested_signer_record_under_the_hub_path_is_read(monkeypatch):
    reads = []
    record = _signer_record(nkey.create(nkey.OPERATOR), nkey.create(nkey.ACCOUNT))
    monkeypatch.setattr(openbao_kv, "read", lambda bao, found: (reads.append(found), (record, 1))[1])
    nats_grant.from_environ({nats_grant.SIGNER_ENV: "agentihooks/hub/a/b", **BAO_ENV})
    assert reads == ["agentihooks/hub/a/b"]


def test_a_signer_record_missing_a_field_is_refused(monkeypatch):
    record = _signer_record(nkey.create(nkey.OPERATOR), nkey.create(nkey.ACCOUNT))
    del record["system_credentials"]
    monkeypatch.setattr(openbao_kv, "read", lambda bao, path: (record, 1))
    environ = {nats_grant.SIGNER_ENV: SIGNER_PATH, **BAO_ENV}
    with pytest.raises(auth.HiveError, match=f"^the NATS signer record {SIGNER_PATH} cannot be used"):
        nats_grant.from_environ(environ)


def test_a_signer_reference_needs_openbao_settings():
    with pytest.raises(auth.HiveError, match="names an OpenBao record"):
        nats_grant.from_environ({nats_grant.SIGNER_ENV: SIGNER_PATH, nats_grant.TRUSTED_OPERATOR_ENV: TRUSTED})


def _signer_dir(tmp_path, record: dict):
    folder = tmp_path / "nats-signer"
    folder.mkdir()
    for name, value in record.items():
        (folder / name).write_text(value)
    (folder / "..data").mkdir()
    return folder


def test_the_signer_comes_from_the_synced_record_folder_named_by_file(tmp_path, monkeypatch):
    operator, account = nkey.create(nkey.OPERATOR), nkey.create(nkey.ACCOUNT)
    reads = []
    monkeypatch.setattr(openbao_kv, "read", lambda bao, found: reads.append(found))
    folder = _signer_dir(tmp_path, _signer_record(operator, account))
    environ = {nats_grant.SIGNER_ENV: f"file:{folder}", nats_grant.TRUSTED_OPERATOR_ENV: TRUSTED}
    assert nats_grant.FILE_SCHEME == "file:"
    assert nats_grant.from_environ(environ) == nats_grant.Signer(operator, account, "c", "u", TRUSTED)
    assert reads == []


def test_a_synced_signer_record_reads_its_member_swarms_as_json(tmp_path):
    record = {
        **_signer_record(nkey.create(nkey.OPERATOR), nkey.create(nkey.ACCOUNT)),
        "member_swarms": '{"h1": ["s1"]}',
    }
    folder = _signer_dir(tmp_path, record)
    environ = {nats_grant.SIGNER_ENV: f"file:{folder}", nats_grant.TRUSTED_OPERATOR_ENV: TRUSTED}
    assert nats_grant.from_environ(environ).member_swarms == {"h1": ("s1",)}


def test_a_synced_signer_needs_the_trusted_operator(tmp_path):
    folder = _signer_dir(tmp_path, _signer_record(nkey.create(nkey.OPERATOR), nkey.create(nkey.ACCOUNT)))
    with pytest.raises(auth.HiveError) as refused:
        nats_grant.from_environ({nats_grant.SIGNER_ENV: f"file:{folder}"})
    assert str(refused.value) == (
        "AGENTIHOOKS_NATS_SIGNER is set, so AGENTIHOOKS_NATS_TRUSTED_OPERATOR must name "
        "the operator public key the NATS server trusts"
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda record: record.pop("system_credentials"),
        lambda record: record.update(operator_seed="nope"),
        lambda record: record.update(member_swarms="{"),
        lambda record: record.update(member_swarms="[]"),
    ],
)
def test_an_unusable_synced_signer_record_is_refused(tmp_path, change):
    record = _signer_record(nkey.create(nkey.OPERATOR), nkey.create(nkey.ACCOUNT))
    change(record)
    folder = _signer_dir(tmp_path, record)
    environ = {nats_grant.SIGNER_ENV: f"file:{folder}", nats_grant.TRUSTED_OPERATOR_ENV: TRUSTED}
    with pytest.raises(
        auth.HiveError, match=f"^(the NATS signer file {folder} cannot be used \\(|member_swarms must map)"
    ):
        nats_grant.from_environ(environ)


def test_a_missing_synced_signer_folder_is_refused(tmp_path):
    environ = {nats_grant.SIGNER_ENV: f"file:{tmp_path / 'gone'}", nats_grant.TRUSTED_OPERATOR_ENV: TRUSTED}
    with pytest.raises(auth.HiveError, match=f"^the NATS signer file {tmp_path / 'gone'} cannot be used \\("):
        nats_grant.from_environ(environ)


def test_refresh_writes_the_bus_credential_beside_the_member_env(tmp_path):
    grant = {"id": "m1", "ledger_credential": "l", "redis_url": "r", "nats_credentials": "creds text"}
    env = auth.write_env(tmp_path, "https://hive", grant).read_text()
    creds = tmp_path / auth.NATS_CREDS_FILE
    assert creds.read_text() == "creds text"
    assert oct(creds.stat().st_mode & 0o777) == "0o600"
    assert env.endswith(f"AGENTIHOOKS_BUS_CREDENTIALS={creds}\nAGENTIHOOKS_BUS_BIND=1\n")
    env = auth.write_env(tmp_path, "https://hive", {**grant, "nats_credentials": None}).read_text()
    assert "AGENTIHOOKS_BUS_CREDENTIALS" not in env
    assert "AGENTIHOOKS_BUS_BIND" not in env
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
    assert [loader() for loader in served] == [signed]


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
    operator = nkey.create(nkey.OPERATOR)
    return nats_grant.Signer(operator, nkey.create(nkey.ACCOUNT), "creds", "nats://hub:4222", operator.public)


def test_a_member_credential_carries_exactly_the_hive_member_claims():
    signed = _offline_signer()
    jwt, user = nkey.parse_creds(nats_grant.mint(signed, "m1"))
    claims = _claims(jwt)
    assert (claims["iss"], claims["sub"], claims["name"]) == (signed.account.public, user.public, "hive-m1")
    buckets = (
        "spoke-work",
        "spoke-pending",
        "hive-presence",
        "hive-executions",
        "execution-starts",
    )
    publish = [
        "swarm.*.execution.>",
        "slots.*.m1",
        "dead.*",
        "dead.*.*",
        "$JS.API.INFO",
        "$JS.API.STREAM.MSG.GET.AGENTIHOOKS_DEAD",
        "$JS.API.CONSUMER.INFO.>",
        "$JS.API.CONSUMER.MSG.NEXT.>",
        "$JS.ACK.>",
        *(f"$KV.{bucket}.>" for bucket in buckets),
        *(f"$JS.API.STREAM.INFO.KV_{bucket}" for bucket in buckets),
        *(f"$JS.API.STREAM.MSG.GET.KV_{bucket}" for bucket in buckets),
    ]
    assert claims["nats"] == {
        "type": "user",
        "version": 2,
        "pub": {"allow": publish},
        "sub": {"allow": [f"_INBOX.{user.public}.>"]},
        "subs": -1,
        "data": -1,
        "payload": -1,
    }
    assert not [subject for subject in publish if ".CREATE" in subject]


def test_a_role_credential_keeps_the_unrestricted_user_claims():
    assert nats_grant.user_claims() == {
        "type": "user",
        "version": 2,
        "pub": {},
        "sub": {},
        "subs": -1,
        "data": -1,
        "payload": -1,
    }


@pytest.mark.parametrize("revocations", [{}, {"UA": 7}])
def test_the_account_jwt_carries_exactly_the_account_claims(revocations):
    signed = _offline_signer()
    claims = _claims(nats_grant.account_jwt(signed.operator, signed.account, revocations))
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
        nkey.create(nkey.OPERATOR),
        nkey.create(nkey.ACCOUNT),
        _user_creds(nkey.create(nkey.ACCOUNT), "sys"),
        "nats://x",
        "unused",
    )
    seen = {}

    class Client:
        async def request(self, subject, payload, **kwargs):
            seen["request"] = (subject, payload, kwargs)
            return SimpleNamespace(data=b'{"data": {"code": 200}}')

        async def close(self):
            seen["closed"] = True

    async def connect(url, **kwargs):
        named = ("allow_reconnect", "connect_timeout", "inbox_prefix")
        seen["connect"] = (url, {key: kwargs[key] for key in named}, sorted(kwargs))
        return Client()

    monkeypatch.setattr(nats_client, "connect", connect)
    nats_grant.push(signed, "a.b.c")
    system = nkey.parse_creds(signed.system_credentials)[1]
    assert seen == {
        "connect": (
            "nats://x",
            {"allow_reconnect": False, "connect_timeout": 10, "inbox_prefix": f"_INBOX.{system.public}"},
            ["allow_reconnect", "connect_timeout", "inbox_prefix", "signature_cb", "user_jwt_cb"],
        ),
        "request": ("$SYS.REQ.CLAIMS.UPDATE", b"a.b.c", {"timeout": 10}),
        "closed": True,
    }
