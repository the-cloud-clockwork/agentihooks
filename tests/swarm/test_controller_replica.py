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
    assert thread.name == replica.BEAT_THREAD


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
def test_the_beat_rides_out_a_redis_error_only_while_the_lock_cannot_have_expired(
    since_take: float, renewals: int
) -> None:
    from redis.exceptions import ConnectionError as RedisDown

    now = [0.0]
    lock = replica.ReplicaLock(_redis(), HIVE, "pod-a", clock=lambda: now[0])
    assert lock.take() is True
    now[0] = since_take
    answers = iter([RedisDown("down"), True, False])
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
    stop.set()


@pytest.mark.feature("controller-standby-replica")
def test_a_lock_is_fresh_until_its_last_take_nears_expiry() -> None:
    now, redis = [100.0], _redis()
    lock = replica.ReplicaLock(redis, HIVE, "pod-a", lock_ms=30_000, clock=lambda: now[0])
    assert lock.fresh(10.0) is False
    assert lock.take() is True
    assert lock.taken_at == 100.0
    now[0] = 119.9
    assert lock.fresh(10.0) is True
    now[0] = 120.0
    assert lock.fresh(10.0) is False
    standby = replica.ReplicaLock(redis, HIVE, "pod-b", clock=lambda: now[0])
    assert standby.take() is False
    assert standby.taken_at == replica.NEVER


@pytest.mark.feature("controller-standby-replica")
def test_a_replica_lock_exists_only_when_the_replica_is_named() -> None:
    redis = _redis()
    assert replica.configured({}, redis, HIVE) is None
    named = replica.configured({replica.REPLICA_ENV: "pod-a"}, redis, HIVE)
    assert named is not None
    assert (named.owner, named.key, named.lock_ms) == ("pod-a", f"{replica.PREFIX}:{HIVE}", replica.LOCK_MS)


@pytest.mark.feature("controller-standby-replica")
def test_the_keeper_calls_its_action_until_stopped() -> None:
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
    assert thread.name == replica.KEEP_THREAD


class _Leader(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length))
        payload = json.dumps(
            {"path": self.path, "authorization": self.headers.get("Authorization"), "body": body}
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
    yield f"http://127.0.0.1:{server.server_address[1]}"
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

    monkeypatch.setattr(readiness, "available", lambda client: available)
    standby = standby_api.Standby(RedisStore(_redis()), "rig")
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
        {"path": "/v2/executions", "authorization": "Bearer grant", "body": {"seat": "eng-1"}},
    )
    assert standby.route("GET", "/v2/executions/x", "Bearer grant", None) == (403, {"error": "refused"})


@pytest.mark.feature("controller-standby-replica")
@pytest.mark.parametrize("method", ["PUT", "PATCH"])
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
    standby_api.publish_leader(store, "rig", "http://127.0.0.1:9")
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
def test_a_standby_serves_its_api_and_published_ports_until_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    import ssl

    from scripts.swarm.store import RedisStore
    from scripts.swarm_v2 import control_service
    from scripts.swarm_v2.api import readiness

    monkeypatch.setattr(readiness, "available", lambda client: True)
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    monkeypatch.setattr(control_service, "published", lambda environ: (("127.0.0.1", 0, context), "https://x"))
    store = RedisStore(_redis())
    servers = control_service.stand_by({control_service.PORT_ENV: "0", control_service.SWARM_ENV: "rig"}, store)
    assert [type(server).__name__ for server in servers] == ["ThreadingHTTPServer", "TLSServer"]
    port = servers[0].server_port
    assert _get(f"http://127.0.0.1:{port}/v2/health/live") == (200, {"probe": "shallow", "status": "standby"})
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
        port = service.server.server_port
        assert store.redis.get(store.key("rig", standby_api.LEADER_KEY)) == f"http://10.0.0.7:{port}"
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
    try:
        assert service.start() is True
        assert store.redis.get(store.key("rig", standby_api.LEADER_KEY)) is None
    finally:
        service.stop()


@pytest.mark.feature("controller-standby-replica")
def test_a_named_replica_stands_by_until_the_leader_releases(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.swarm import controller
    from scripts.swarm.store import RedisStore

    store = RedisStore(_redis())
    leader = replica.ReplicaLock(store.redis, HIVE, "pod-a")
    assert leader.take() is True
    waits: list[float] = []

    def wait(seconds: float) -> None:
        waits.append(seconds)
        assert leader.release() is True

    stop = threading.Event()
    monkeypatch.setattr(controller, "STOPPING", stop)
    lock = replica.ReplicaLock(store.redis, HIVE, "pod-b")
    controller._lead(store, {}, lock, wait)
    try:
        assert (lock.owner, lock.holder(), waits) == ("pod-b", "pod-b", [replica.POLL_S])
        assert capsys.readouterr().out == (
            "controller: replica pod-b stands by while pod-a leads\ncontroller: replica pod-b leads the passes\n"
        )
        assert any(thread.name == replica.BEAT_THREAD for thread in threading.enumerate())
    finally:
        stop.set()


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
