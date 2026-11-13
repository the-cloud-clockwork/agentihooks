import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from scripts.swarm import replica

pytestmark = pytest.mark.unit

HIVE = "agentihooks-swarm"


def _redis() -> Any:
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


def _no_wait(seconds: float) -> None:
    assert seconds == replica.POLL_S
    time.sleep(0.001)


@pytest.mark.feature("controller-standby-replica")
def test_two_replicas_never_run_a_pass_at_the_same_time() -> None:
    redis = _redis()
    guard, active, peak, passes = threading.Lock(), [0], [0], []

    def run(owner: str) -> None:
        lock = replica.ReplicaLock(redis, HIVE, owner)
        for _ in range(5):
            lock.lead(_no_wait)
            with guard:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.002)
            with guard:
                active[0] -= 1
                passes.append(owner)
            assert lock.release() is True

    threads = [threading.Thread(target=run, args=(owner,)) for owner in ("pod-a", "pod-b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert peak[0] == 1
    assert sorted(passes) == ["pod-a"] * 5 + ["pod-b"] * 5


@pytest.mark.feature("controller-standby-replica")
def test_the_standby_takes_over_once_the_leader_releases() -> None:
    redis = _redis()
    leader = replica.ReplicaLock(redis, HIVE, "pod-a")
    standby = replica.ReplicaLock(redis, HIVE, "pod-b")
    assert leader.take() is True
    assert leader.take() is True
    assert standby.take() is False
    assert standby.holder() == "pod-a"
    assert standby.release() is False
    assert leader.release() is True
    assert standby.take() is True
    assert leader.take() is False
    assert redis.pttl(f"{replica.PREFIX}:{HIVE}") > replica.LOCK_MS - 1000


@pytest.mark.feature("controller-standby-replica")
def test_the_standby_takes_over_once_the_leader_lock_expires() -> None:
    redis = _redis()
    leader = replica.ReplicaLock(redis, HIVE, "pod-a", lock_ms=5)
    standby = replica.ReplicaLock(redis, HIVE, "pod-b")
    assert leader.take() is True
    time.sleep(0.05)
    assert standby.take() is True
    assert leader.take() is False


@pytest.mark.feature("controller-standby-replica")
def test_a_replica_lock_is_scoped_to_its_hive() -> None:
    redis = _redis()
    assert replica.ReplicaLock(redis, HIVE, "pod-a").take() is True
    assert replica.ReplicaLock(redis, "another-hive", "pod-b").take() is True


@pytest.mark.feature("controller-standby-replica")
def test_the_beat_renews_the_lock_until_stopped() -> None:
    redis = _redis()
    lock = replica.ReplicaLock(redis, HIVE, "pod-a", lock_ms=60)
    assert lock.take() is True
    stop, lost = threading.Event(), threading.Event()
    thread = lock.beat(lost.set, stop, beat_s=0.01)
    time.sleep(0.2)
    assert replica.ReplicaLock(redis, HIVE, "pod-b").take() is False
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not lost.is_set()
    assert (thread.name, thread.daemon) == (replica.BEAT_THREAD, True)


@pytest.mark.feature("controller-standby-replica")
def test_the_beat_reports_a_lost_lock_once_and_ends() -> None:
    redis = _redis()
    lock = replica.ReplicaLock(redis, HIVE, "pod-a")
    assert lock.take() is True
    redis.set(f"{replica.PREFIX}:{HIVE}", "pod-b")
    stop, calls = threading.Event(), []
    thread = lock.beat(lambda: calls.append(1), stop, beat_s=0.01)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert calls == [1]
    stop.set()


@pytest.mark.feature("controller-standby-replica")
@pytest.mark.parametrize(("since_take", "renewals"), [(5.0, 3), (29.995, 1)])
@pytest.mark.parametrize("failure", ["redis", "other"])
def test_the_beat_rides_out_a_renewal_error_only_while_the_lock_cannot_have_expired(
    since_take: float, renewals: int, failure: str, capsys: pytest.CaptureFixture[str]
) -> None:
    from redis.exceptions import ConnectionError as RedisDown

    now = [0.0]
    lock = replica.ReplicaLock(_redis(), HIVE, "pod-a", clock=lambda: now[0])
    assert lock.take() is True
    now[0] = since_take
    error = RedisDown("down") if failure == "redis" else RuntimeError("broken")
    answers = iter([error, True, False])
    asked: list[int] = []

    def take() -> bool:
        asked.append(1)
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    lock.take = take  # type: ignore[method-assign]
    stop, calls = threading.Event(), []
    thread = lock.beat(lambda: calls.append(1), stop, beat_s=0.01)
    thread.join(timeout=5)
    assert (calls, len(asked)) == ([1], renewals)
    assert capsys.readouterr() == ("", f"controller: renewing the replica lock failed: {error}\n")
    stop.set()


@pytest.mark.feature("controller-standby-replica")
def test_a_lock_is_fresh_until_its_last_take_nears_expiry() -> None:
    now, redis = [100.0], _redis()
    lock = replica.ReplicaLock(redis, HIVE, "pod-a", lock_ms=30_000, clock=lambda: now[0])
    assert lock.fresh(10.0) is False
    assert lock.take() is True
    assert lock.taken_at == 100.0
    now[0] = 119.98
    assert lock.fresh(10.0) is True
    now[0] = 120.0
    assert lock.fresh(10.0) is False
    standby = replica.ReplicaLock(redis, HIVE, "pod-b", clock=lambda: now[0])
    assert standby.take() is False
    assert standby.taken_at == replica.NEVER


@pytest.mark.feature("controller-standby-replica")
def test_a_take_counts_from_before_its_round_trip() -> None:
    times = iter([100.0, 107.0])
    lock = replica.ReplicaLock(_redis(), HIVE, "pod-a", clock=lambda: next(times))
    assert lock.take() is True
    assert lock.taken_at == 100.0


class _CountingStore:
    def __init__(self, redis: Any) -> None:
        self.client, self.reached = redis, 0

    @property
    def redis(self) -> Any:
        self.reached += 1
        return self.client


@pytest.mark.feature("controller-standby-replica")
@pytest.mark.parametrize("environ", [{}, {replica.REPLICA_ENV: ""}])
def test_an_unnamed_replica_has_no_lock_and_never_reaches_redis(environ: dict[str, str]) -> None:
    store = _CountingStore(_redis())
    assert replica.configured(environ, store, lambda: pytest.fail("the hive id was read")) is None
    assert store.reached == 0


@pytest.mark.feature("controller-standby-replica")
def test_a_named_replica_locks_its_hive() -> None:
    redis = _redis()
    store = _CountingStore(redis)
    named = replica.configured({replica.REPLICA_ENV: "pod-a"}, store, lambda: HIVE)
    assert named is not None
    assert (named.redis, store.reached) == (redis, 1)
    assert (named.owner, named.key, named.lock_ms) == ("pod-a", f"{replica.PREFIX}:{HIVE}", replica.LOCK_MS)
    assert named.holder() == ""


@pytest.mark.feature("controller-standby-replica")
def test_the_keeper_calls_its_action_until_stopped(capsys: pytest.CaptureFixture[str]) -> None:
    stop, calls = threading.Event(), []

    def keep() -> None:
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("renewal failed")
        if len(calls) == 3:
            stop.set()

    thread = replica.keep(keep, stop, every_s=0.01)
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert calls == [1, 1, 1]
    assert (thread.name, thread.daemon) == (replica.KEEP_THREAD, True)
    assert capsys.readouterr() == ("", "controller: keeping the controller credential failed: renewal failed\n")


class _Leader(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length))
        payload = json.dumps(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "type": self.headers.get("Content-Type"),
                "body": body,
            }
        ).encode()
        self.send_response(201)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        payload = b'{"error": "refused"}'
        self.send_response(403)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_PUT(self) -> None:
        self._page(200)

    def do_PATCH(self) -> None:
        self._page(502)

    def do_DELETE(self) -> None:
        self.send_response(500)
        self.send_header("Content-Length", "100")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b'{"err')

    def _page(self, status: int) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        payload = b"<html>bad gateway</html>"
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def leader() -> Any:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Leader)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.mark.feature("controller-standby-replica")
@pytest.mark.parametrize(
    ("available", "ready"), [(True, (200, {"redis": "ok"})), (False, (503, {"redis": "unavailable"}))]
)
def test_a_standby_answers_health_itself(
    monkeypatch: pytest.MonkeyPatch, available: bool, ready: tuple[int, dict[str, str]]
) -> None:
    from scripts.swarm.store import RedisStore
    from scripts.swarm_v2 import standby as standby_api
    from scripts.swarm_v2.api import readiness

    redis = _redis()
    monkeypatch.setattr(readiness, "available", lambda client: client is redis and available)
    standby = standby_api.Standby(RedisStore(redis), "rig")
    assert standby.route("GET", "/v2/health/live", "None", None) == (200, {"probe": "shallow", "status": "standby"})
    assert standby.route("GET", "/v2/health/ready", "None", None) == ready


@pytest.mark.feature("controller-standby-replica")
def test_a_standby_forwards_every_other_request_to_the_leader(leader: str) -> None:
    from scripts.swarm.store import RedisStore
    from scripts.swarm_v2 import standby as standby_api

    store = RedisStore(_redis())
    standby_api.publish_leader(store, "rig", leader)
    standby = standby_api.Standby(store, "rig")
    assert standby.route("POST", "/v2/executions", "Bearer grant", {"seat": "eng-1"}) == (
        201,
        {
            "path": "/v2/executions",
            "authorization": "Bearer grant",
            "type": "application/json",
            "body": {"seat": "eng-1"},
        },
    )
    assert standby.route("GET", "/v2/executions/x", "Bearer grant", None) == (403, {"error": "refused"})


@pytest.mark.feature("controller-standby-replica")
@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_a_standby_refuses_a_leader_reply_that_is_not_json(leader: str, method: str) -> None:
    from scripts.swarm.store import RedisStore
    from scripts.swarm_v2 import standby as standby_api

    store = RedisStore(_redis())
    standby_api.publish_leader(store, "rig", leader)
    assert standby_api.Standby(store, "rig").route(method, "/v2/executions/x", "Bearer grant", {}) == (
        503,
        {
            "error_class": "dependency_unavailable",
            "operation_id": "unknown",
            "retry": "same_request",
            "message": "the leading controller replica did not answer",
        },
    )


@pytest.mark.feature("controller-standby-replica")
def test_a_published_standby_forwards_only_the_recovery_paths(leader: str) -> None:
    from scripts.swarm.store import RedisStore
    from scripts.swarm_v2 import standby as standby_api
    from scripts.swarm_v2.api.recovery import PATH

    store = RedisStore(_redis())
    standby_api.publish_leader(store, "rig", leader)
    published = standby_api.Standby(store, "rig", PATH)
    assert published.route("POST", "/v2/executions", "Bearer grant", {}) == (
        404,
        {
            "error_class": "invalid_request",
            "operation_id": "unknown",
            "retry": "new_request",
            "message": "no such endpoint on the published port",
        },
    )
    assert published.route("GET", "/v2/health/live", "None", None)[0] == 404
    path = "/v1/executions/x/restore-decision"
    assert published.route("POST", path, "Bearer grant", {"choice": "resume"}) == (
        201,
        {"path": path, "authorization": "Bearer grant", "type": "application/json", "body": {"choice": "resume"}},
    )


@pytest.mark.feature("controller-standby-replica")
def test_a_standby_refuses_while_no_replica_leads() -> None:
    from scripts.swarm.store import RedisStore
    from scripts.swarm_v2 import standby as standby_api

    standby = standby_api.Standby(RedisStore(_redis()), "rig")
    assert standby.route("POST", "/v2/executions", "Bearer grant", {}) == (
        503,
        {
            "error_class": "dependency_unavailable",
            "operation_id": "unknown",
            "retry": "same_request",
            "message": "no controller replica leads the swarm yet",
        },
    )


@pytest.mark.feature("controller-standby-replica")
def test_a_standby_refuses_when_the_leader_is_unreachable() -> None:
    from scripts.swarm.store import RedisStore
    from scripts.swarm_v2 import standby as standby_api

    store = RedisStore(_redis())
    standby_api.publish_leader(store, "rig", "127.0.0.1:9")
    standby = standby_api.Standby(store, "rig")
    assert standby.route("POST", "/v2/executions", "Bearer grant", {}) == (
        503,
        {
            "error_class": "dependency_unavailable",
            "operation_id": "unknown",
            "retry": "same_request",
            "message": "the leading controller replica did not answer",
        },
    )


def _get(url: str) -> tuple[int, dict]:
    import urllib.request

    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, json.loads(response.read())


@pytest.mark.feature("controller-standby-replica")
def test_a_standby_serves_its_api_and_published_ports_until_closed(
    monkeypatch: pytest.MonkeyPatch, leader: str
) -> None:
    import ssl
    import urllib.request

    from scripts.swarm.store import RedisStore
    from scripts.swarm_v2 import control_service
    from scripts.swarm_v2 import standby as standby_api
    from scripts.swarm_v2.api.recovery import PATH

    context, asked = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH), []
    environ = {control_service.PORT_ENV: "0", control_service.SWARM_ENV: "rig"}
    monkeypatch.setattr(
        control_service, "published", lambda given: asked.append(given) or (("127.0.0.1", 0, context), "https://x")
    )
    store = RedisStore(_redis())
    standby_api.publish_leader(store, "rig", leader)
    before = set(threading.enumerate())
    servers = control_service.stand_by(environ, store)
    started = [thread for thread in threading.enumerate() if thread not in before]
    assert asked == [environ]
    assert [type(server).__name__ for server in servers] == ["ThreadingHTTPServer", "TLSServer"]
    routers = [server.RequestHandlerClass.api for server in servers]  # type: ignore[attr-defined]
    assert [(router.store, router.slug, router.only) for router in routers] == [
        (store, "rig", None),
        (store, "rig", PATH),
    ]
    assert sorted((thread.name, thread.daemon) for thread in started) == [("swarm-v2-api-standby", True)] * 2
    port = servers[0].server_port
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v2/executions",
        json.dumps({"seat": "eng-1"}).encode(),
        {"Authorization": "Bearer grant", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        assert (response.status, json.loads(response.read())["body"]) == (201, {"seat": "eng-1"})
    control_service.close(servers)
    with pytest.raises(OSError):
        _get(f"http://127.0.0.1:{port}/v2/health/live")


@pytest.mark.feature("controller-standby-replica")
@pytest.mark.parametrize(
    "environ", [{}, {"AGENTIHOOKS_CONTROL_API_PORT": "8780"}, {"AGENTIHOOKS_CONTROL_SWARM": "rig"}]
)
def test_a_standby_without_an_api_serves_nothing(environ: dict[str, str]) -> None:
    from scripts.swarm.store import RedisStore
    from scripts.swarm_v2 import control_service

    assert control_service.stand_by(environ, RedisStore(_redis())) == []


@pytest.mark.feature("controller-standby-replica")
def test_the_leading_control_service_publishes_its_pod_address(tmp_path: Any) -> None:
    from scripts.swarm.store import RedisStore, SwarmConfig
    from scripts.swarm_v2 import control_service
    from scripts.swarm_v2 import standby as standby_api

    key_path = tmp_path / "launch.key"
    key_path.write_bytes(b"k" * 32)
    environ = {control_service.KEY_ID_ENV: "launch-1", control_service.KEY_FILE_ENV: str(key_path)}
    store = RedisStore(_redis())
    store.create(SwarmConfig("rig", "agentihooks", 2, 0))
    service = control_service.ControlService(store, "rig", control_service.launch_key(environ), lambda: True)
    service.listen, service.address = ("127.0.0.1", 0), "10.0.0.7"
    try:
        assert service.start() is True
        assert service.server is not None
        port = service.server.server_port
        assert store.redis.get(store.key("rig", standby_api.LEADER_KEY)) == f"10.0.0.7:{port}"
        assert store.redis.pttl(store.key("rig", standby_api.LEADER_KEY)) > 0
    finally:
        service.stop()


@pytest.mark.feature("controller-standby-replica")
def test_a_control_service_without_a_pod_address_publishes_none(tmp_path: Any) -> None:
    from scripts.swarm.store import RedisStore, SwarmConfig
    from scripts.swarm_v2 import control_service
    from scripts.swarm_v2 import standby as standby_api

    key_path = tmp_path / "launch.key"
    key_path.write_bytes(b"k" * 32)
    environ = {control_service.KEY_ID_ENV: "launch-1", control_service.KEY_FILE_ENV: str(key_path)}
    store = RedisStore(_redis())
    store.create(SwarmConfig("rig", "agentihooks", 2, 0))
    service = control_service.ControlService(store, "rig", control_service.launch_key(environ), lambda: True)
    service.listen = ("127.0.0.1", 0)
    assert service.address == ""
    try:
        assert service.start() is True
        assert store.redis.get(store.key("rig", standby_api.LEADER_KEY)) is None
    finally:
        service.stop()


@pytest.mark.feature("controller-standby-replica")
def test_hosting_takes_the_pod_address_the_leader_publishes(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    from scripts.swarm.store import RedisStore
    from scripts.swarm_v2 import control_service

    monkeypatch.setattr(control_service.ControlService, "start", lambda self: True)
    key_path = tmp_path / "launch.key"
    key_path.write_bytes(b"k" * 32)
    base = {
        control_service.KEY_ID_ENV: "launch-1",
        control_service.KEY_FILE_ENV: str(key_path),
        control_service.PORT_ENV: "8780",
        control_service.SWARM_ENV: "rig",
        control_service.CREDENTIAL_ENV: "credential",
        "LEDGER_DIR": str(tmp_path / "ledger"),
    }
    addresses = []
    for environ in (base, {**base, control_service.POD_IP_ENV: "10.0.0.7"}):
        service = control_service.host(environ, RedisStore(_redis()), "hive")
        assert service is not None
        addresses.append(service.address)
    assert addresses == ["", "10.0.0.7"]


@pytest.mark.feature("controller-standby-replica")
def test_a_named_replica_stands_by_until_the_leader_releases(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.swarm import controller
    from scripts.swarm.store import RedisStore
    from scripts.swarm_v2 import control_service

    store = RedisStore(_redis())
    leader = replica.ReplicaLock(store.redis, HIVE, "pod-a")
    assert leader.take() is True
    waits: list[float] = []
    events: list[Any] = []

    def wait(seconds: float) -> None:
        waits.append(seconds)
        assert leader.release() is True

    environ = {"AGENTIHOOKS_CONTROL_API_PORT": "8780"}
    monkeypatch.setattr(control_service, "stand_by", lambda given, at: events.append(("serve", given, at)) or ["api"])
    monkeypatch.setattr(control_service, "close", lambda servers: events.append(("close", servers)))
    stop = threading.Event()
    monkeypatch.setattr(controller, "STOPPING", stop)
    lock = replica.ReplicaLock(store.redis, HIVE, "pod-b")
    monkeypatch.setattr(lock, "beat", lambda lost, stopping: events.append(("beat", lost, stopping)))
    controller._lead(store, environ, lock, wait)
    assert (lock.owner, lock.holder(), waits) == ("pod-b", "pod-b", [replica.POLL_S])
    assert events == [("serve", environ, store), ("close", ["api"]), ("beat", controller._lost, stop)]
    assert capsys.readouterr().out == (
        "controller: replica pod-b stands by while pod-a leads\ncontroller: replica pod-b leads the passes\n"
    )


@pytest.mark.feature("controller-standby-replica")
def test_passes_hand_the_replica_lock_to_every_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import controller, redis_grace

    given: list[Any] = []
    monkeypatch.setattr(controller, "_finishing", lambda *args: given.append(args))
    monkeypatch.setattr(redis_grace, "cleared", lambda marker: None)
    lock = replica.ReplicaLock(_redis(), HIVE, "pod-a")
    outage = redis_grace.Outage.from_environ()
    assert controller._passes("store", None, "proofs", True, outage, lock) == 0  # type: ignore[arg-type]
    assert given == [("store", None, "proofs", lock)]


@pytest.mark.feature("controller-standby-replica")
def test_controller_run_stands_by_then_releases_its_replica_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import signal

    from scripts import operator_env
    from scripts.swarm import commands, controller
    from scripts.swarm.store import RedisStore
    from scripts.swarm_v2 import control_service, proof_swarm

    store, calls = RedisStore(_redis()), []
    monkeypatch.setattr(controller, "STOPPING", threading.Event())
    monkeypatch.setattr(controller, "DRAINING", threading.Event())
    monkeypatch.setenv(replica.REPLICA_ENV, "pod-a")
    monkeypatch.setattr(operator_env, "fill", lambda env: None)
    monkeypatch.setattr(operator_env, "classifier", lambda env: [])
    monkeypatch.setattr(controller, "connect", lambda: store)
    monkeypatch.setattr(commands, "hive_id", lambda: HIVE)
    monkeypatch.setattr(signal, "signal", lambda number, handler: "previous")
    monkeypatch.setattr(proof_swarm, "run", lambda env, at: calls.append(("proofs", at)))
    monkeypatch.setattr(control_service, "host", lambda env, at, hive: calls.append(("host", at, hive)))
    monkeypatch.setattr(controller, "_keep_credential", lambda env, at: calls.append(("keep", env, at)))

    def lead(at: Any, env: Any, lock: replica.ReplicaLock) -> None:
        assert lock.take() is True
        calls.append(("lead", at, env, lock.owner, lock.key))

    def passes(*args: Any) -> int:
        calls.append(("passes", args[0], args[1], args[3], args[5].owner, args[5].holder()))
        return 0

    monkeypatch.setattr(controller, "_lead", lead)
    monkeypatch.setattr(controller, "_passes", passes)
    assert controller.main(["run", "--once"]) == 0
    assert calls == [
        ("keep", os.environ, store),
        ("lead", store, os.environ, "pod-a", f"{replica.PREFIX}:{HIVE}"),
        ("proofs", store),
        ("host", store, HIVE),
        ("passes", store, None, True, "pod-a", "pod-a"),
    ]
    assert replica.ReplicaLock(store.redis, HIVE, "pod-a").holder() == ""
    assert controller.STOPPING.is_set()


@pytest.mark.feature("controller-standby-replica")
def test_controller_run_without_a_named_replica_never_stands_by(monkeypatch: pytest.MonkeyPatch) -> None:
    import signal

    from scripts import operator_env
    from scripts.swarm import controller
    from scripts.swarm_v2 import control_service, proof_swarm

    monkeypatch.setattr(controller, "STOPPING", threading.Event())
    monkeypatch.setattr(controller, "DRAINING", threading.Event())
    monkeypatch.delenv(replica.REPLICA_ENV, raising=False)
    monkeypatch.setattr(operator_env, "fill", lambda env: None)
    monkeypatch.setattr(operator_env, "classifier", lambda env: [])
    monkeypatch.setattr(controller, "connect", lambda: "store")
    monkeypatch.setattr(signal, "signal", lambda number, handler: "previous")
    monkeypatch.setattr(proof_swarm, "run", lambda env, at: None)
    monkeypatch.setattr(control_service, "host", lambda env, at, hive: None)
    monkeypatch.setattr(controller, "_lead", lambda *args: pytest.fail("a replica without a name stood by"))
    seen: list[Any] = []
    monkeypatch.setattr(controller, "_passes", lambda *args: seen.append(args[5]) or 0)
    assert controller.main(["run", "--once"]) == 0
    assert seen == [None]


@pytest.mark.feature("controller-standby-replica")
def test_a_named_replica_with_no_leader_leads_without_standing_by(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.swarm import controller
    from scripts.swarm.store import RedisStore

    stop = threading.Event()
    monkeypatch.setattr(controller, "STOPPING", stop)
    try:
        lock = replica.ReplicaLock(_redis(), HIVE, "pod-a")
        controller._lead(RedisStore(lock.redis), {}, lock, _no_wait)
        assert lock.holder() == "pod-a"
        assert capsys.readouterr().out == "controller: replica pod-a leads the passes\n"
    finally:
        stop.set()


@pytest.mark.feature("controller-standby-replica")
def test_a_replica_that_lost_the_lead_exits_at_once(capfd: pytest.CaptureFixture[str]) -> None:
    from scripts.swarm import controller

    ends: list[int] = []
    controller._lost(ends.append)
    assert ends == [controller.LOST_EXIT] == [75]
    assert capfd.readouterr().err == "controller: another replica took the lead; exiting\n"


class _Ended(Exception):
    pass


@pytest.mark.feature("controller-standby-replica")
def test_a_replica_renews_its_lock_before_every_pass_and_never_runs_one_without_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.swarm import controller

    redis, passes = _redis(), []
    monkeypatch.setattr(controller, "_pass", lambda store, service, proof_swarm: passes.append(store))

    def end() -> None:
        raise _Ended

    monkeypatch.setattr(controller, "_lost", end)
    lock = replica.ReplicaLock(redis, HIVE, "pod-a")
    assert lock.take() is True
    controller._finishing("store", None, None, lock)  # type: ignore[arg-type]
    assert passes == ["store"]
    assert redis.pttl(lock.key) > replica.LOCK_MS - 1000
    redis.set(lock.key, "pod-b")
    with pytest.raises(_Ended):
        controller._finishing("store", None, None, lock)  # type: ignore[arg-type]
    assert passes == ["store"]
    controller._finishing("store", None, None)  # type: ignore[arg-type]
    assert passes == ["store", "store"]


@pytest.mark.feature("controller-standby-replica")
def test_a_replica_keeps_its_own_controller_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.hive import auth
    from scripts.swarm import controller
    from scripts.swarm.store import RedisStore

    store = RedisStore(_redis())
    kept: list[tuple[Any, str]] = []
    stop = threading.Event()
    monkeypatch.setattr(controller, "STOPPING", stop)
    monkeypatch.setattr(replica, "KEEP_S", 0.01)
    monkeypatch.setattr(auth, "keep_service", lambda redis, credential: kept.append((redis, credential)) or stop.set())
    thread = controller._keep_credential({"AGENTIHOOKS_CONTROLLER_CREDENTIAL": "controller-pod-a.secret"}, store)
    assert thread is not None
    thread.join(timeout=5)
    assert kept == [(store.redis, "controller-pod-a.secret")]


@pytest.mark.feature("controller-standby-replica")
@pytest.mark.parametrize("environ", [{}, {"AGENTIHOOKS_CONTROLLER_CREDENTIAL": "sharedsecret"}])
def test_a_shared_controller_credential_needs_no_keeping(environ: dict[str, str]) -> None:
    from scripts.swarm import controller
    from scripts.swarm.store import RedisStore

    assert controller._keep_credential(environ, RedisStore(_redis())) is None
