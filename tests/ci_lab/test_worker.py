"""Tests of the CI lab worker pod loop against the real lease API over fakeredis and a presigned upload sink."""

import threading
import time
import urllib.error
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts.ci_lab import lease_api, leases, queue, worker
from scripts.ci_lab.units import Unit

CAPABILITIES = frozenset({"net", "browser"})
UNIT_A = Unit(kind="test", key="a", members=("tests/a.py::t",))
UNIT_B = Unit(kind="test", key="b", members=("tests/b.py::t",))


class Clock:
    def __init__(self) -> None:
        self.now = 1000

    def __call__(self) -> int:
        return self.now

    def sleep(self, ms: int) -> None:
        self.now += ms


@dataclass
class Counted(leases.Leases):
    beats: list[str] = field(default_factory=list[str])
    asked: list[tuple[str, int, frozenset[str], int]] = field(
        default_factory=list[tuple[str, int, frozenset[str], int]]
    )

    def lease(self, worker: str, free_slots: int, capabilities: frozenset[str], wait_ms: int) -> list[leases.Grant]:
        self.asked.append((worker, free_slots, capabilities, wait_ms))
        return super().lease(worker, free_slots, capabilities, wait_ms)

    def heartbeat(self, job: str, token: str) -> int | None:
        self.beats.append(job)
        return super().heartbeat(job, token)


class Sink:
    def __init__(self) -> None:
        self.bodies: dict[str, bytes] = {}

        def put(request: BaseHTTPRequestHandler) -> None:
            self.bodies[request.path] = request.rfile.read(int(request.headers["Content-Length"]))
            request.send_response(200)
            request.send_header("Content-Length", "0")
            request.end_headers()

        handler = type("SinkHandler", (BaseHTTPRequestHandler,), {"do_PUT": put})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


class Signer:
    def __init__(self, url: str) -> None:
        self.url = url

    def upload_url(self, job: str, name: str) -> str:
        return f"{self.url}/{job}/{name}"


def _serving(server: ThreadingHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def jobs() -> queue.Queue:
    import fakeredis

    return queue.Queue(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))


@pytest.fixture
def lab(jobs: queue.Queue, clock: Clock) -> Counted:
    return Counted(jobs, ttl_ms=500, clock=clock, sleep=clock.sleep, poll_ms=50)


@pytest.fixture
def sink() -> Iterator[Sink]:
    sink = Sink()
    _serving(sink.server)
    yield sink
    sink.server.shutdown()
    sink.server.server_close()


@pytest.fixture
def scheduler(lab: Counted, sink: Sink) -> Iterator[str]:
    server = lease_api.serve("127.0.0.1", 0, lab, Signer(sink.url))
    _serving(server)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def finished(root: Path, lease: worker.Lease, outcome: str = "passed") -> worker.Finished:
    report = root / f"{lease.unit.key}.json"
    report.write_text(f'{{"key": "{lease.unit.key}"}}')
    log = root / f"{lease.unit.key}.txt"
    log.write_text(f"log of {lease.head}")
    return worker.Finished(outcome=outcome, report=report, log=log)


def pod(
    scheduler: str,
    executors: dict[str, worker.Executor],
    slots: int = 1,
    heartbeat_s: float = worker.HEARTBEAT_SECONDS,
    grace_s: float = worker.GRACE_SECONDS,
) -> worker.Worker:
    return worker.Worker(
        worker.Client(scheduler), executors, "pod-1", slots, wait_ms=0, heartbeat_s=heartbeat_s, grace_s=grace_s
    )


def found(jobs: queue.Queue, job: str) -> queue.Job:
    record = jobs.job(job)
    assert record is not None
    return record


def beating(lab: Counted, count: int) -> bool:
    deadline = time.monotonic() + 10
    while len(lab.beats) < count and time.monotonic() < deadline:
        time.sleep(0.005)
    return len(lab.beats) >= count


@pytest.mark.feature("lab-worker-pod-loop")
def test_units_from_two_heads_run_concurrently_on_one_pod(
    scheduler: str, lab: Counted, jobs: queue.Queue, sink: Sink, tmp_path: Path
) -> None:
    jobs.enqueue([UNIT_A], "h1", {})
    jobs.enqueue([UNIT_B], "h2", {})
    meet = threading.Barrier(2, timeout=10)

    def together(lease: worker.Lease) -> worker.Finished:
        meet.wait()
        return finished(tmp_path, lease)

    two = pod(scheduler, {"test": together}, slots=2)

    started = two.run_once()

    assert sorted(lease.head for lease in started) == ["h1", "h2"]
    assert two.drain() == []
    assert [found(jobs, job).outcome for job in ("h1:test:a", "h2:test:b")] == ["passed", "passed"]
    assert sink.bodies == {
        "/h1:test:a/report": b'{"key": "a"}',
        "/h1:test:a/log": b"log of h1",
        "/h2:test:b/report": b'{"key": "b"}',
        "/h2:test:b/log": b"log of h2",
    }
    assert lab.asked == [("pod-1", 2, CAPABILITIES, 0)]


@pytest.mark.feature("lab-worker-pod-loop")
def test_a_crash_stops_the_heartbeat_and_the_lease_reaps(
    scheduler: str, lab: Counted, jobs: queue.Queue, clock: Clock, sink: Sink, capsys: pytest.CaptureFixture[str]
) -> None:
    jobs.enqueue([UNIT_A], "h1", {})

    def crash(lease: worker.Lease) -> worker.Finished:
        beating(lab, 2)
        raise RuntimeError("unit process crashed")

    crashing = pod(scheduler, {"test": crash}, heartbeat_s=0.01)

    crashing.run_once()

    assert crashing.drain() == []
    beats = len(lab.beats)
    time.sleep(0.2)
    assert len(lab.beats) == beats >= 2
    assert found(jobs, "h1:test:a").state == "leased"
    assert sink.bodies == {}
    assert "ci-lab worker: unit h1:test:a crashed: RuntimeError('unit process crashed')\n" in capsys.readouterr().err
    clock.now += lab.ttl_ms + 1
    assert lab.reap(clock.now) == [("h1:test:a", "pending")]


@pytest.mark.feature("lab-worker-pod-loop")
def test_a_refused_heartbeat_ends_the_units_heartbeats_and_its_delivery(
    scheduler: str,
    lab: Counted,
    jobs: queue.Queue,
    sink: Sink,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    jobs.enqueue([UNIT_A], "h1", {})
    release = threading.Event()

    def held(lease: worker.Lease) -> worker.Finished:
        release.wait(10)
        return finished(tmp_path, lease)

    holding = pod(scheduler, {"test": held}, heartbeat_s=0.01)
    [lease] = holding.run_once()
    assert beating(lab, 1)

    assert jobs.finish(found(jobs, lease.job), lease.token, "taken")
    time.sleep(0.2)
    beats = len(lab.beats)
    time.sleep(0.2)
    assert len(lab.beats) == beats
    release.set()

    assert holding.drain() == []
    assert sink.bodies == {}
    assert found(jobs, lease.job).outcome == "taken"
    assert "crashed" not in capsys.readouterr().err


@pytest.mark.feature("lab-worker-pod-loop")
def test_stop_lets_running_units_finish_within_the_termination_grace(
    scheduler: str, jobs: queue.Queue, tmp_path: Path
) -> None:
    jobs.enqueue([UNIT_A], "h1", {})
    release = threading.Event()

    def held(lease: worker.Lease) -> worker.Finished:
        release.wait(10)
        return finished(tmp_path, lease)

    stopping = pod(scheduler, {"test": held}, grace_s=10)
    [lease] = stopping.run_once()
    stopping.stop()
    threading.Timer(0.1, release.set).start()

    assert stopping.run() == []
    assert found(jobs, lease.job).outcome == "passed"


@pytest.mark.feature("lab-worker-pod-loop")
def test_a_unit_past_the_termination_grace_is_left_to_reap(scheduler: str, jobs: queue.Queue, tmp_path: Path) -> None:
    jobs.enqueue([UNIT_A], "h1", {})
    release = threading.Event()

    def held(lease: worker.Lease) -> worker.Finished:
        release.wait(10)
        return finished(tmp_path, lease)

    late = pod(scheduler, {"test": held}, grace_s=0.05)
    [lease] = late.run_once()
    late.stop()
    timer = threading.Timer(2, release.set)
    timer.start()

    assert late.run() == [lease]
    assert found(jobs, lease.job).state == "leased"
    release.set()
    timer.cancel()
    late.grace_s = 10
    assert late.drain() == []
    assert found(jobs, lease.job).outcome == "passed"


@pytest.mark.feature("lab-worker-pod-loop")
def test_run_leases_again_as_a_unit_frees_its_slot(
    scheduler: str, lab: Counted, jobs: queue.Queue, tmp_path: Path
) -> None:
    jobs.enqueue([UNIT_A], "h1", {})
    jobs.enqueue([UNIT_B], "h2", {})
    executors: dict[str, worker.Executor] = {}
    single = pod(scheduler, executors)

    def last(lease: worker.Lease) -> worker.Finished:
        if lease.head == "h2":
            single.stop()
        return finished(tmp_path, lease)

    executors["test"] = last
    guard = threading.Timer(10, single.stop)
    guard.start()

    assert single.run() == []
    guard.cancel()
    assert [found(jobs, job).outcome for job in ("h1:test:a", "h2:test:b")] == ["passed", "passed"]
    assert len(lab.asked) >= 2
    assert {free for _, free, _, _ in lab.asked} == {1}


@pytest.mark.feature("lab-worker-pod-loop")
def test_the_pod_leases_only_units_its_net_and_browser_capabilities_and_free_slots_fit(
    scheduler: str, jobs: queue.Queue, tmp_path: Path
) -> None:
    wide = Unit(kind="test", key="c", members=("tests/c.py::t",))
    requirements = {
        "a": queue.Requirement(slots=2, capabilities=frozenset({"net", "browser"})),
        "b": queue.Requirement(slots=1, capabilities=frozenset({"gpu"})),
        "c": queue.Requirement(slots=2),
    }
    jobs.enqueue([UNIT_A, UNIT_B, wide], "h1", requirements)
    release = threading.Event()

    def held(lease: worker.Lease) -> worker.Finished:
        release.wait(10)
        return finished(tmp_path, lease)

    three = pod(scheduler, {"test": held}, slots=3)

    assert [lease.unit for lease in three.run_once()] == [UNIT_A]
    assert three.free() == 1
    release.set()
    assert three.drain() == []
    assert three.free() == 3
    assert found(jobs, "h1:test:b").state == "pending"
    assert found(jobs, "h1:test:c").state == "pending"


@pytest.mark.feature("lab-worker-pod-loop")
def test_the_client_speaks_the_lease_api_and_reads_a_refused_token_as_none(
    scheduler: str, jobs: queue.Queue, clock: Clock, sink: Sink
) -> None:
    jobs.enqueue([UNIT_A], "h1", {})
    client = worker.Client(scheduler)

    [lease] = client.lease("pod-1", 1, CAPABILITIES, 0)

    assert lease == worker.Lease(job="h1:test:a", token=lease.token, head="h1", unit=UNIT_A, slots=1, expires_ms=1500)
    assert client.heartbeat(lease.job, "wrong") is None
    assert client.upload_url(lease.job, "wrong", "report") is None
    assert client.complete(lease.job, "wrong", "passed") is False
    clock.now = 1200
    assert client.heartbeat(lease.job, lease.token) == 1700
    assert client.upload_url(lease.job, lease.token, "log") == f"{sink.url}/h1:test:a/log"
    with pytest.raises(urllib.error.HTTPError) as error:
        client.upload_url(lease.job, lease.token, "coverage")
    assert error.value.code == 400
    assert client.complete(lease.job, lease.token, "passed") is True
    assert found(jobs, lease.job).outcome == "passed"


@pytest.mark.feature("lab-worker-pod-loop")
def test_the_worker_environment_refuses_github_app_and_redis_variables() -> None:
    environ = {
        "CI_LAB_SCHEDULER_URL": "http://scheduler:8800",
        "GITHUB_APP_ID": "1",
        "CI_LAB_REDIS_URL": "redis://redis:6379",
        "redis_host": "redis",
    }

    with pytest.raises(worker.Refused) as refused:
        worker.environment(environ)

    assert str(refused.value) == "ci-lab worker refuses CI_LAB_REDIS_URL, GITHUB_APP_ID, redis_host"


@pytest.mark.feature("lab-worker-pod-loop")
def test_the_worker_environment_keeps_only_the_service_contract_variables() -> None:
    contract = {
        "CI_LAB_SCHEDULER_URL": "http://scheduler:8800",
        "CI_LAB_SLOTS": "2",
        "CI_LAB_GIT_MIRROR": "/mirror",
        "UV_CACHE_DIR": "/uv",
        "CI_LAB_MINIO_ENDPOINT": "http://minio:9000",
        "AGENTIHOOKS_BUS_URL": "nats://bus:4222",
    }

    assert worker.environment({**contract, "HOME": "/root", "PATH": "/bin"}) == contract


@pytest.mark.feature("lab-worker-pod-loop")
@pytest.mark.parametrize(
    ("environ", "cpus", "quota", "expected"),
    [
        ({}, 8, 2.5, 2),
        ({}, 4, None, 4),
        ({}, 8, 0.5, 1),
        ({"CI_LAB_SLOTS": "1"}, 8, 4.0, 1),
        ({"CI_LAB_SLOTS": "6"}, 8, 2.0, 2),
    ],
)
def test_slots_come_from_the_cpu_quota_capped_by_ci_lab_slots(
    environ: dict[str, str], cpus: int, quota: float | None, expected: int
) -> None:
    assert worker.slots(environ, cpus, quota) == expected


@pytest.mark.feature("lab-worker-pod-loop")
def test_build_makes_the_pod_worker_from_a_clean_environment_and_refuses_redis() -> None:
    environ = {"CI_LAB_SCHEDULER_URL": "http://scheduler:8800", "CI_LAB_SLOTS": "3", "HOME": "/root"}
    executors: dict[str, worker.Executor] = {}

    built = worker.build(environ, executors, "pod-7", 8, 6.0)

    assert (built.client, built.executors, built.name, built.slots) == (
        worker.Client("http://scheduler:8800"),
        executors,
        "pod-7",
        3,
    )
    assert built.capabilities == CAPABILITIES
    with pytest.raises(worker.Refused):
        worker.build({**environ, "REDIS_URL": "redis://redis"}, executors, "pod-7", 8, 6.0)
