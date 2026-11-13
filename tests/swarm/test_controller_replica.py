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
def test_the_beat_rides_out_a_redis_error() -> None:
    redis = _redis()
    lock = replica.ReplicaLock(redis, HIVE, "pod-a")
    from redis.exceptions import ConnectionError as RedisDown

    answers = iter([RedisDown("down"), True, False])

    def take() -> bool:
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    lock.take = take  # type: ignore[method-assign]
    stop, calls = threading.Event(), []
    thread = lock.beat(lambda: calls.append(1), stop, beat_s=0.01)
    thread.join(timeout=5)
    assert calls == [1]
    stop.set()


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
@pytest.mark.parametrize(("available", "ready"), [(True, (200, {"redis": "ok"})), (False, (503, {"redis": "unavailable"}))])
def test_a_standby_answers_health_itself(
    monkeypatch: pytest.MonkeyPatch, available: bool, ready: tuple[int, dict[str, str]]
) -> None:
    from scripts.swarm.store import RedisStore
    from scripts.swarm_v2 import control_service
    from scripts.swarm_v2.api import readiness

    monkeypatch.setattr(readiness, "available", lambda client: available)
    standby = control_service.Standby(RedisStore(_redis()), "rig")
    assert standby.route("GET", "/v2/health/live", "None", None) == (200, {"probe": "shallow", "status": "standby"})
    assert standby.route("GET", "/v2/health/ready", "None", None) == ready


@pytest.mark.feature("controller-standby-replica")
def test_a_standby_forwards_every_other_request_to_the_leader(leader: str) -> None:
    from scripts.swarm.store import RedisStore
    from scripts.swarm_v2 import control_service

    store = RedisStore(_redis())
    control_service.publish_leader(store, "rig", leader)
    standby = control_service.Standby(store, "rig")
    assert standby.route("POST", "/v2/executions", "Bearer grant", {"seat": "eng-1"}) == (
        201,
        {"path": "/v2/executions", "authorization": "Bearer grant", "body": {"seat": "eng-1"}},
    )
    assert standby.route("GET", "/v2/executions/x", "Bearer grant", None) == (403, {"error": "refused"})


@pytest.mark.feature("controller-standby-replica")
def test_a_standby_refuses_while_no_replica_leads() -> None:
    from scripts.swarm.store import RedisStore
    from scripts.swarm_v2 import control_service

    standby = control_service.Standby(RedisStore(_redis()), "rig")
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
    from scripts.swarm_v2 import control_service

    store = RedisStore(_redis())
    control_service.publish_leader(store, "rig", "http://127.0.0.1:9")
    standby = control_service.Standby(store, "rig")
    assert standby.route("POST", "/v2/executions", "Bearer grant", {}) == (
        503,
        {
            "error_class": "dependency_unavailable",
            "operation_id": "unknown",
            "retry": "same_request",
            "message": "the leading controller replica did not answer",
        },
    )
