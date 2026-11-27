"""Tests of the CI lab worker pod loop against the real lease API over fakeredis and a presigned upload sink."""

import email.message
import http.client
import json
import threading
import time
import urllib.error
from collections.abc import Iterator
from concurrent.futures import Future, wait
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from scripts.ci_lab import lease_api, leases, queue, worker
from scripts.ci_lab.units import Unit

CAPABILITIES = frozenset({"net", "browser"})
UNIT_A = Unit(kind="test", key="a", members=("tests/a.py::t",))
UNIT_B = Unit(kind="test", key="b", members=("tests/b.py::t",))
EXPECTED_GRACE_SECONDS = 2_100.0


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
    owned: list[str] = field(default_factory=list[str])
    asked: list[tuple[str, int, frozenset[str], int]] = field(
        default_factory=list[tuple[str, int, frozenset[str], int]]
    )

    def lease(self, worker: str, free_slots: int, capabilities: frozenset[str], wait_ms: int) -> list[leases.Grant]:
        self.asked.append((worker, free_slots, capabilities, wait_ms))
        return super().lease(worker, free_slots, capabilities, wait_ms)

    def heartbeat(self, job: str, token: str) -> int | None:
        self.beats.append(job)
        return super().heartbeat(job, token)

    def owns(self, job: str, token: str) -> bool:
        self.owned.append(job)
        return super().owns(job, token)


@dataclass(frozen=True)
class Calls(worker.Client):
    calls: list[str] = field(default_factory=list[str])

    def upload_url(self, job: str, token: str, name: str) -> str | None:
        self.calls.append("upload_url")
        return super().upload_url(job, token, name)

    def put(self, url: str, path: Path) -> None:
        self.calls.append("put")
        super().put(url, path)

    def complete(self, job: str, token: str, outcome: str) -> bool:
        self.calls.append("complete")
        return super().complete(job, token, outcome)


class SinkServer(ThreadingHTTPServer):
    request_queue_size = 40


class Sink:
    def __init__(self) -> None:
        self.bodies: dict[str, bytes] = {}

        def put(request: BaseHTTPRequestHandler) -> None:
            self.bodies[request.path] = request.rfile.read(int(request.headers["Content-Length"]))
            request.send_response(200)
            request.send_header("Content-Length", "0")
            request.end_headers()

        handler = type("SinkHandler", (BaseHTTPRequestHandler,), {"do_PUT": put})
        self.server = SinkServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


class Signer:
    def __init__(self, url: str) -> None:
        self.url = url

    def upload_url(self, job: str, name: str) -> str:
        return f"{self.url}/{job}/{name}"


class Slow:
    def __init__(self) -> None:
        self.release = threading.Event()

        def hold(request: BaseHTTPRequestHandler) -> None:
            request.rfile.read(int(request.headers.get("Content-Length", "0")))
            self.release.wait(5)
            request.send_response(200)
            request.send_header("Content-Length", "2")
            request.end_headers()
            request.wfile.write(b"{}")

        handler = type("SlowHandler", (BaseHTTPRequestHandler,), {"do_GET": hold, "do_POST": hold, "do_PUT": hold})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


class Paced(threading.Event):
    def __init__(self) -> None:
        super().__init__()
        self.waits: list[float | None] = []

    def wait(self, timeout: float | None = None) -> bool:
        self.waits.append(timeout)
        return self.is_set()


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
def slow() -> Iterator[Slow]:
    slow = Slow()
    _serving(slow.server)
    yield slow
    slow.release.set()
    slow.server.shutdown()
    slow.server.server_close()


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
    tmp_path: Path,
    slots: int = 1,
    heartbeat_s: float = worker.HEARTBEAT_SECONDS,
    grace_s: float = worker.GRACE_SECONDS,
) -> worker.Worker:
    return worker.Worker(
        worker.Client(scheduler),
        executors,
        "pod-1",
        slots,
        tmp_path,
        wait_ms=0,
        heartbeat_s=heartbeat_s,
        grace_s=grace_s,
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

    def together(lease: worker.Lease, root: Path) -> worker.Finished:
        meet.wait()
        return finished(tmp_path, lease)

    two = pod(scheduler, {"test": together}, tmp_path, slots=2)

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
def test_every_slot_runs_a_unit_at_once(scheduler: str, jobs: queue.Queue, tmp_path: Path) -> None:
    keys = [f"u{index}" for index in range(40)]
    jobs.enqueue([Unit(kind="test", key=key, members=(f"tests/{key}.py::t",)) for key in keys], "h1", {})
    meet = threading.Barrier(40, timeout=10)

    def together(lease: worker.Lease, root: Path) -> worker.Finished:
        meet.wait()
        return finished(tmp_path, lease)

    wide = pod(scheduler, {"test": together}, tmp_path, slots=40, grace_s=30)

    assert len(wide.run_once()) == 40
    assert wide.drain() == []
    assert [found(jobs, f"h1:test:{key}").outcome for key in keys] == ["passed"] * 40


@pytest.mark.feature("lab-worker-sink-backlog")
def test_the_sink_listens_with_room_for_forty_uploads(monkeypatch: pytest.MonkeyPatch) -> None:
    backlogs: list[int] = []
    activate = ThreadingHTTPServer.server_activate

    def observed(server: ThreadingHTTPServer) -> None:
        backlogs.append(server.request_queue_size)
        activate(server)

    monkeypatch.setattr(ThreadingHTTPServer, "server_activate", observed)
    upload = Sink()
    upload.server.server_close()

    assert len(backlogs) == 1
    assert backlogs[0] >= 40


@pytest.mark.feature("lab-worker-pod-loop")
def test_a_full_pod_waits_for_its_first_unit_to_end_while_the_second_still_runs(
    scheduler: str, lab: Counted, jobs: queue.Queue, tmp_path: Path
) -> None:
    jobs.enqueue([UNIT_A], "h1", {})
    jobs.enqueue([UNIT_B], "h2", {})
    releases = {"h1": threading.Event(), "h2": threading.Event()}

    def held(lease: worker.Lease, root: Path) -> worker.Finished:
        releases[lease.head].wait(10)
        return finished(tmp_path, lease)

    full = pod(scheduler, {"test": held}, tmp_path, slots=2)
    assert len(full.run_once()) == 2
    threading.Timer(0.3, releases["h1"].set).start()

    assert full.run_once() == []

    assert full.free() == 1
    assert [lease.head for lease in full.running.values()] == ["h2"]
    assert found(jobs, "h1:test:a").outcome == "passed"
    assert len(lab.asked) == 1
    releases["h2"].set()
    assert full.drain() == []


@pytest.mark.feature("lab-worker-pod-loop")
def test_a_crash_stops_the_heartbeat_and_the_lease_reaps(
    scheduler: str,
    lab: Counted,
    jobs: queue.Queue,
    clock: Clock,
    sink: Sink,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    jobs.enqueue([UNIT_A], "h1", {})

    def crash(lease: worker.Lease, root: Path) -> worker.Finished:
        beating(lab, 2)
        raise RuntimeError("unit process crashed")

    crashing = pod(scheduler, {"test": crash}, tmp_path, heartbeat_s=0.01)

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


@pytest.mark.feature("lab-worker-crash-traceback")
def test_a_crash_logs_the_traceback_naming_the_missing_path(
    scheduler: str, jobs: queue.Queue, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jobs.enqueue([UNIT_A], "h1", {})
    missing = tmp_path / "tree" / "uv.lock"

    def crash(lease: worker.Lease, root: Path) -> worker.Finished:
        missing.read_bytes()
        return finished(tmp_path, lease)

    crashing = pod(scheduler, {"test": crash}, tmp_path)

    crashing.run_once()

    assert crashing.drain() == []
    line = "ci-lab worker: unit h1:test:a crashed: FileNotFoundError(2, 'No such file or directory')\n"
    _, crashed, trace = capsys.readouterr().err.partition(line)
    assert crashed == line
    assert trace.startswith("Traceback (most recent call last):\n")
    assert ", in crash\n" in trace
    assert trace.endswith(f"FileNotFoundError: [Errno 2] No such file or directory: {str(missing)!r}\n")


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

    def held(lease: worker.Lease, root: Path) -> worker.Finished:
        release.wait(10)
        return finished(tmp_path, lease)

    holding = pod(scheduler, {"test": held}, tmp_path, heartbeat_s=0.01)
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
    assert lab.owned == []
    assert found(jobs, lease.job).outcome == "taken"
    assert "crashed" not in capsys.readouterr().err


@pytest.mark.feature("lab-worker-pod-loop")
def test_a_heartbeat_that_cannot_reach_the_scheduler_is_skipped_and_the_unit_delivered(
    scheduler: str, lab: Counted, jobs: queue.Queue, sink: Sink, tmp_path: Path
) -> None:
    jobs.enqueue([UNIT_A], "h1", {})
    failed: list[str] = []

    class Flaky(worker.Client):
        def heartbeat(self, job: str, token: str) -> int | None:
            if not failed:
                failed.append(job)
                raise urllib.error.URLError("scheduler unreachable")
            return super().heartbeat(job, token)

    def held(lease: worker.Lease, root: Path) -> worker.Finished:
        beating(lab, 1)
        return finished(tmp_path, lease)

    flaky = worker.Worker(Flaky(scheduler), {"test": held}, "pod-1", 1, tmp_path, wait_ms=0, heartbeat_s=0.01)
    flaky.run_once()

    assert flaky.drain() == []
    assert failed == ["h1:test:a"]
    assert len(lab.beats) >= 1
    assert found(jobs, "h1:test:a").outcome == "passed"
    assert sink.bodies == {"/h1:test:a/report": b'{"key": "a"}', "/h1:test:a/log": b"log of h1"}


@pytest.mark.feature("lab-worker-pod-loop")
def test_a_heartbeat_whose_reply_is_cut_short_is_skipped_and_the_unit_delivered(
    scheduler: str, lab: Counted, jobs: queue.Queue, sink: Sink, tmp_path: Path
) -> None:
    jobs.enqueue([UNIT_A], "h1", {})
    failed: list[str] = []

    class Cut(worker.Client):
        def heartbeat(self, job: str, token: str) -> int | None:
            if not failed:
                failed.append(job)
                raise http.client.IncompleteRead(b"{")
            return super().heartbeat(job, token)

    def held(lease: worker.Lease, root: Path) -> worker.Finished:
        beating(lab, 1)
        return finished(tmp_path, lease)

    cut = worker.Worker(Cut(scheduler), {"test": held}, "pod-1", 1, tmp_path, wait_ms=0, heartbeat_s=0.01)
    cut.run_once()

    assert cut.drain() == []
    assert failed == ["h1:test:a"]
    assert len(lab.beats) >= 1
    assert found(jobs, "h1:test:a").outcome == "passed"
    assert sink.bodies == {"/h1:test:a/report": b'{"key": "a"}', "/h1:test:a/log": b"log of h1"}


@pytest.mark.feature("lab-worker-pod-loop")
def test_a_refused_upload_url_ends_the_units_delivery(
    scheduler: str, jobs: queue.Queue, sink: Sink, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jobs.enqueue([UNIT_A], "h1", {})

    def taken(lease: worker.Lease, root: Path) -> worker.Finished:
        assert jobs.finish(found(jobs, lease.job), lease.token, "taken")
        return finished(tmp_path, lease)

    refused = pod(scheduler, {"test": taken}, tmp_path)
    refused.run_once()

    assert refused.drain() == []
    assert sink.bodies == {}
    assert found(jobs, "h1:test:a").outcome == "taken"
    assert "crashed" not in capsys.readouterr().err


@pytest.mark.feature("lab-worker-pod-loop")
def test_stop_lets_running_units_finish_within_the_termination_grace(
    scheduler: str, jobs: queue.Queue, tmp_path: Path
) -> None:
    jobs.enqueue([UNIT_A], "h1", {})
    release = threading.Event()
    executors: dict[str, worker.Executor] = {}
    stopping = pod(scheduler, executors, tmp_path, slots=2, grace_s=10)

    def held(lease: worker.Lease, root: Path) -> worker.Finished:
        stopping.stop()
        release.wait(10)
        return finished(tmp_path, lease)

    executors["test"] = held
    threading.Timer(0.2, release.set).start()
    guard = threading.Timer(10, stopping.stop)
    guard.start()

    assert stopping.run() == []
    guard.cancel()
    assert found(jobs, "h1:test:a").outcome == "passed"


@pytest.mark.feature("lab-worker-pod-loop")
def test_a_unit_past_the_termination_grace_is_left_to_reap(
    scheduler: str, lab: Counted, jobs: queue.Queue, clock: Clock, sink: Sink, tmp_path: Path
) -> None:
    jobs.enqueue([UNIT_A], "h1", {})
    release = threading.Event()
    executors: dict[str, worker.Executor] = {}
    late = pod(scheduler, executors, tmp_path, slots=2, heartbeat_s=0.01, grace_s=0.05)

    def held(lease: worker.Lease, root: Path) -> worker.Finished:
        beating(lab, 1)
        late.stop()
        release.wait(10)
        return finished(tmp_path, lease)

    executors["test"] = held
    guard = threading.Timer(10, late.stop)
    guard.start()

    assert [lease.job for lease in late.run()] == ["h1:test:a"]
    guard.cancel()
    time.sleep(0.2)
    beats = len(lab.beats)
    time.sleep(0.2)
    assert len(lab.beats) == beats >= 1
    release.set()
    late.grace_s = 10
    assert late.drain() == []
    assert sink.bodies == {}
    assert found(jobs, "h1:test:a").state == "leased"
    clock.now += lab.ttl_ms + 1
    assert lab.reap(clock.now) == [("h1:test:a", "pending")]


@pytest.mark.feature("lab-worker-pod-loop")
def test_a_built_worker_waits_out_its_longest_unit_and_its_delivery_after_stop(
    scheduler: str, jobs: queue.Queue, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs.enqueue([UNIT_A], "h1", {})
    release = threading.Event()
    waits: list[float | None] = []

    def held(lease: worker.Lease, root: Path) -> worker.Finished:
        release.wait(10)
        return finished(tmp_path, lease)

    def virtual(
        futures: list[Future[None]], timeout: float | None = None
    ) -> tuple[set[Future[None]], set[Future[None]]]:
        waits.append(timeout)
        if timeout is not None and timeout >= EXPECTED_GRACE_SECONDS:
            release.set()
            return wait(futures, timeout=10)
        return wait(futures, timeout=0)

    monkeypatch.setattr(worker, "wait", virtual)
    built = worker.build({"CI_LAB_SCHEDULER_URL": scheduler}, {"test": held}, "pod-1", 1, None, tmp_path)

    assert len(built.run_once()) == 1
    built.stop()
    left = built.run()
    release.set()

    assert left == []
    assert waits == [EXPECTED_GRACE_SECONDS]
    assert found(jobs, "h1:test:a").outcome == "passed"


@pytest.mark.feature("lab-worker-pod-loop")
def test_the_termination_grace_counts_every_delivery_call_of_a_unit(
    scheduler: str, jobs: queue.Queue, tmp_path: Path
) -> None:
    jobs.enqueue([UNIT_A], "h1", {})
    client = Calls(scheduler)

    def done(lease: worker.Lease, root: Path) -> worker.Finished:
        return finished(tmp_path, lease)

    one = worker.Worker(client, {"test": done}, "pod-1", 1, tmp_path, wait_ms=0, grace_s=10)
    one.run_once()

    assert one.drain() == []
    assert client.calls == ["upload_url", "put", "upload_url", "put", "complete"]
    assert len(client.calls) == worker.DELIVERY_CALLS
    assert client.timeout == 60.0


@pytest.mark.feature("lab-worker-pod-loop")
def test_run_leases_again_as_a_unit_frees_its_slot(
    scheduler: str, lab: Counted, jobs: queue.Queue, tmp_path: Path
) -> None:
    jobs.enqueue([UNIT_A], "h1", {})
    jobs.enqueue([UNIT_B], "h2", {})
    executors: dict[str, worker.Executor] = {}
    single = pod(scheduler, executors, tmp_path)

    def last(lease: worker.Lease, root: Path) -> worker.Finished:
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
def test_a_lease_call_the_scheduler_does_not_answer_backs_off_and_leases_again(
    scheduler: str, jobs: queue.Queue, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jobs.enqueue([UNIT_A], "h1", {})
    jobs.enqueue([UNIT_B], "h2", {})
    refused = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    failed = urllib.error.HTTPError(f"{scheduler}/lease", 500, "Internal Server Error", email.message.Message(), None)
    unanswered: list[Exception | None] = [
        refused,
        http.client.RemoteDisconnected("Remote end closed connection without response"),
        ConnectionResetError(104, "Connection reset by peer"),
        http.client.IncompleteRead(b"{"),
        TimeoutError("timed out"),
        failed,
        None,
        refused,
        None,
    ]

    class Rolling(worker.Client):
        def lease(self, worker: str, free_slots: int, capabilities: frozenset[str], wait_ms: int) -> list[worker.Lease]:
            if unanswered and (error := unanswered.pop(0)) is not None:
                raise error
            return super().lease(worker, free_slots, capabilities, wait_ms)

    executors: dict[str, worker.Executor] = {}
    paced = Paced()
    rolling = worker.Worker(Rolling(scheduler), executors, "pod-1", 1, tmp_path, wait_ms=0, stopping=paced)

    def last(lease: worker.Lease, root: Path) -> worker.Finished:
        if lease.head == "h2":
            rolling.stop()
        return finished(tmp_path, lease)

    executors["test"] = last
    guard = threading.Timer(10, rolling.stop)
    guard.start()

    assert rolling.run_once() == []
    assert rolling.run() == []
    guard.cancel()
    assert unanswered == []
    assert paced.waits == [1.0, 2.0, 4.0, 8.0, 10.0, 10.0, 1.0]
    assert [found(jobs, job).outcome for job in ("h1:test:a", "h2:test:b")] == ["passed", "passed"]
    assert [line for line in capsys.readouterr().err.splitlines() if line.startswith("ci-lab worker:")] == [
        "ci-lab worker: lease call failed, retrying in 1s: URLError(ConnectionRefusedError(111, 'Connection refused'))",
        "ci-lab worker: lease call failed, retrying in 2s: RemoteDisconnected('Remote end closed connection without response')",
        "ci-lab worker: lease call failed, retrying in 4s: ConnectionResetError(104, 'Connection reset by peer')",
        "ci-lab worker: lease call failed, retrying in 8s: IncompleteRead(1 bytes read)",
        "ci-lab worker: lease call failed, retrying in 10s: TimeoutError('timed out')",
        "ci-lab worker: lease call failed, retrying in 10s: <HTTPError 500: 'Internal Server Error'>",
        "ci-lab worker: lease call failed, retrying in 1s: URLError(ConnectionRefusedError(111, 'Connection refused'))",
    ]


@pytest.mark.feature("lab-worker-pod-loop")
def test_a_lease_call_the_scheduler_answers_with_a_client_error_still_raises(
    scheduler: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paced = Paced()
    misrouted = worker.Worker(
        worker.Client(f"{scheduler}/elsewhere"), {}, "pod-1", 1, tmp_path, wait_ms=0, stopping=paced
    )

    with pytest.raises(urllib.error.HTTPError) as error:
        misrouted.run_once()

    assert error.value.code == 404
    assert paced.waits == []
    assert "lease call failed" not in capsys.readouterr().err


@pytest.mark.feature("lab-worker-seat-keeping-restart")
def test_a_lease_call_the_scheduler_answers_426_stops_the_worker_without_raising_or_backing_off(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class Stale(worker.Client):
        def lease(self, worker: str, free_slots: int, capabilities: frozenset[str], wait_ms: int) -> list[worker.Lease]:
            raised = email.message.Message()
            raise urllib.error.HTTPError("http://scheduler:8800/lease", 426, "Upgrade Required", raised, None)

    paced = Paced()
    stale = worker.Worker(Stale("http://scheduler:8800"), {}, "pod-1", 1, tmp_path, wait_ms=0, stopping=paced)

    assert stale.run_once() == []

    assert stale.stopping.is_set()
    assert paced.waits == []
    logged = capsys.readouterr().err
    assert "lease call failed" not in logged
    assert "ci-lab worker: the lab wants another image, restarting\n" in logged


@pytest.mark.feature("lab-worker-seat-keeping-restart")
def test_a_stale_restart_lets_a_running_unit_finish_within_the_grace_then_run_returns(
    scheduler: str, jobs: queue.Queue, tmp_path: Path
) -> None:
    jobs.enqueue([UNIT_A], "h1", {})
    release = threading.Event()
    calls = [0]

    def held(lease: worker.Lease, root: Path) -> worker.Finished:
        release.wait(10)
        return finished(tmp_path, lease)

    class Stale(worker.Client):
        def lease(self, worker: str, free_slots: int, capabilities: frozenset[str], wait_ms: int) -> list[worker.Lease]:
            calls[0] += 1
            if calls[0] == 1:
                return super().lease(worker, free_slots, capabilities, wait_ms)
            raised = email.message.Message()
            raise urllib.error.HTTPError(f"{scheduler}/lease", 426, "Upgrade Required", raised, None)

    restarting = worker.Worker(Stale(scheduler), {"test": held}, "pod-1", 2, tmp_path, wait_ms=0, grace_s=10)
    assert len(restarting.run_once()) == 1

    threading.Timer(0.2, release.set).start()
    guard = threading.Timer(10, restarting.stop)
    guard.start()

    assert restarting.run() == []
    guard.cancel()
    assert found(jobs, "h1:test:a").outcome == "passed"


@pytest.mark.wall_clock
@pytest.mark.feature("lab-worker-pod-loop")
def test_stop_ends_a_lease_backoff_at_once(tmp_path: Path) -> None:
    class Down(worker.Client):
        def lease(self, worker: str, free_slots: int, capabilities: frozenset[str], wait_ms: int) -> list[worker.Lease]:
            threading.Timer(0.1, down.stop).start()
            raise ConnectionRefusedError(111, "Connection refused")

    down = worker.Worker(Down("http://scheduler:8800"), {}, "pod-1", 1, tmp_path, wait_ms=0)
    down.backoff_s = 60.0
    began = time.monotonic()

    assert down.run() == []
    assert time.monotonic() - began < 30


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

    def held(lease: worker.Lease, root: Path) -> worker.Finished:
        release.wait(10)
        return finished(tmp_path, lease)

    three = pod(scheduler, {"test": held}, tmp_path, slots=3)

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
    jobs.enqueue([UNIT_A], "h1", {}, "b1", "g1")
    client = worker.Client(scheduler)

    [lease] = client.lease("pod-1", 1, CAPABILITIES, 0)

    assert lease == worker.Lease("h1:test:a", lease.token, "h1", UNIT_A, 1, 1500, "b1", "g1")
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


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_the_client_reads_the_bundle_routes_404_and_refused_token_as_none(scheduler: str, jobs: queue.Queue) -> None:
    jobs.enqueue([UNIT_A], "h1", {}, "b1", "g1", graded_commit="m" * 40)
    client = worker.Client(scheduler)
    [lease] = client.lease("pod-1", 1, CAPABILITIES, 0)
    assert client.bundle(lease.job, "wrong", "m" * 40) is None
    assert client.bundle(lease.job, lease.token, "m" * 40) is None


@pytest.mark.feature("lab-grades-the-pull-request-merge")
def test_the_client_lets_a_bundle_routes_server_error_raise(lab: Counted, sink: Sink, jobs: queue.Queue) -> None:
    def reply(method: str, target: str, raw: bytes, authorization: str | None) -> lease_api.Reply:
        if (method, target) == lease_api.BUNDLE_ROUTE:
            return 500, {"error": "boom"}
        return lease_api.answer(lab, Signer(sink.url), method, target, raw, authorization)

    server = lease_api.bind("127.0.0.1", 0, reply)
    _serving(server)
    try:
        jobs.enqueue([UNIT_A], "h1", {}, "b1", "g1", graded_commit="m" * 40)
        client = worker.Client(f"http://127.0.0.1:{server.server_address[1]}")
        [lease] = client.lease("pod-1", 1, CAPABILITIES, 0)
        with pytest.raises(urllib.error.HTTPError) as error:
            client.bundle(lease.job, lease.token, "m" * 40)
        assert error.value.code == 500
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.feature("lab-worker-pod-loop")
def test_the_lease_request_body_carries_exactly_worker_free_slots_capabilities_and_wait_ms() -> None:
    seen: list[bytes] = []

    def reply(method: str, target: str, raw: bytes, authorization: str | None) -> tuple[int, dict[str, Any]]:
        seen.append(raw)
        return 200, {"leases": []}

    server = lease_api.bind("127.0.0.1", 0, reply)
    _serving(server)
    try:
        client = worker.Client(f"http://127.0.0.1:{server.server_address[1]}")
        client.lease("pod-1", 2, frozenset({"net"}), 10)
    finally:
        server.shutdown()
        server.server_close()

    [raw] = seen
    assert json.loads(raw) == {"worker": "pod-1", "free_slots": 2, "capabilities": ["net"], "wait_ms": 10}


@pytest.mark.feature("lab-worker-pod-loop")
def test_the_client_gives_up_on_a_scheduler_or_upload_that_does_not_answer(slow: Slow, tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text("{}")
    client = worker.Client(slow.url, timeout=0.05)

    with pytest.raises(TimeoutError):
        client.heartbeat("h1:test:a", "token")
    with pytest.raises(TimeoutError):
        client.put(f"{slow.url}/h1:test:a/report", report)


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
def test_build_makes_the_pod_worker_from_a_clean_environment_and_refuses_redis(tmp_path: Path) -> None:
    environ = {"CI_LAB_SCHEDULER_URL": "http://scheduler:8800", "CI_LAB_SLOTS": "3", "HOME": "/root"}
    executors: dict[str, worker.Executor] = {}

    built = worker.build(environ, executors, "pod-7", 8, 6.0, tmp_path)

    assert (built.client, built.executors, built.name, built.slots, built.runs) == (
        worker.Client("http://scheduler:8800"),
        executors,
        "pod-7",
        3,
        tmp_path,
    )
    assert built.capabilities == CAPABILITIES
    assert (
        worker.build({"CI_LAB_SCHEDULER_URL": "http://scheduler:8800"}, executors, "pod-7", 8, 2.0, tmp_path).slots == 2
    )
    with pytest.raises(worker.Refused):
        worker.build({**environ, "REDIS_URL": "redis://redis"}, executors, "pod-7", 8, 6.0, tmp_path)


@pytest.mark.feature("lab-unit-outcomes")
def test_an_executor_answer_with_an_unknown_outcome_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError) as refused:
        worker.Finished(outcome="taken", report=tmp_path / "report.json", log=tmp_path / "log.txt")

    assert str(refused.value) == "unknown lab unit outcome 'taken'"
    kept = worker.Finished(outcome="gate_failed", report=tmp_path / "report.json", log=tmp_path / "log.txt")
    assert kept.outcome == "gate_failed"


@pytest.mark.feature("lab-worker-run-directory-reclaimed")
def test_a_delivered_units_lease_root_is_removed_and_its_report_and_log_upload_whole(
    scheduler: str, jobs: queue.Queue, sink: Sink, tmp_path: Path
) -> None:
    jobs.enqueue([UNIT_A], "h1", {})
    seen: list[Path] = []
    report_body = '{"key": "a", "padding": "' + ("x" * 500) + '"}'
    log_body = "log of h1\n" + ("y" * 500)

    def held(lease: worker.Lease, root: Path) -> worker.Finished:
        seen.append(root)
        report = root / "report.json"
        report.write_text(report_body)
        log = root / "log.txt"
        log.write_text(log_body)
        return worker.Finished(outcome="passed", report=report, log=log)

    runs = tmp_path / "runs"
    runs.mkdir()
    one = worker.Worker(
        worker.Client(scheduler),
        {"test": held},
        "pod-1",
        1,
        runs,
        wait_ms=0,
    )

    one.run_once()

    assert one.drain() == []
    [root] = seen
    assert not root.exists()
    assert list(runs.iterdir()) == []
    assert sink.bodies == {"/h1:test:a/report": report_body.encode(), "/h1:test:a/log": log_body.encode()}
    assert found(jobs, "h1:test:a").outcome == "passed"


@pytest.mark.feature("lab-worker-run-directory-reclaimed")
def test_a_units_lease_root_is_removed_even_though_its_executor_raises_and_its_lease_reaps(
    scheduler: str, lab: Counted, jobs: queue.Queue, clock: Clock, sink: Sink, tmp_path: Path
) -> None:
    jobs.enqueue([UNIT_A], "h1", {})
    seen: list[Path] = []

    def crash(lease: worker.Lease, root: Path) -> worker.Finished:
        seen.append(root)
        raise RuntimeError("unit process crashed")

    runs = tmp_path / "runs"
    runs.mkdir()
    crashing = worker.Worker(
        worker.Client(scheduler),
        {"test": crash},
        "pod-1",
        1,
        runs,
        wait_ms=0,
        heartbeat_s=0.01,
    )

    crashing.run_once()

    assert crashing.drain() == []
    [root] = seen
    assert not root.exists()
    assert list(runs.iterdir()) == []
    assert sink.bodies == {}
    clock.now += lab.ttl_ms + 1
    assert lab.reap(clock.now) == [("h1:test:a", "pending")]


@pytest.mark.feature("lab-worker-run-directory-reclaimed")
def test_a_units_lease_root_is_removed_though_a_lost_lease_delivers_nothing(
    scheduler: str, lab: Counted, jobs: queue.Queue, sink: Sink, tmp_path: Path
) -> None:
    jobs.enqueue([UNIT_A], "h1", {})
    release = threading.Event()
    seen: list[Path] = []

    def held(lease: worker.Lease, root: Path) -> worker.Finished:
        seen.append(root)
        release.wait(10)
        report = root / "report.json"
        report.write_text('{"key": "a"}')
        log = root / "log.txt"
        log.write_text("log of h1")
        return worker.Finished(outcome="passed", report=report, log=log)

    runs = tmp_path / "runs"
    runs.mkdir()
    holding = worker.Worker(
        worker.Client(scheduler),
        {"test": held},
        "pod-1",
        1,
        runs,
        wait_ms=0,
        heartbeat_s=0.01,
    )
    [lease] = holding.run_once()
    assert beating(lab, 1)
    assert jobs.finish(found(jobs, lease.job), lease.token, "taken")
    release.set()

    assert holding.drain() == []
    [root] = seen
    assert not root.exists()
    assert list(runs.iterdir()) == []
    assert sink.bodies == {}


@pytest.mark.feature("lab-worker-run-directory-reclaimed")
def test_a_units_lease_root_holding_a_read_only_folder_is_fully_removed(
    scheduler: str, jobs: queue.Queue, sink: Sink, tmp_path: Path
) -> None:
    jobs.enqueue([UNIT_A], "h1", {})
    seen: list[Path] = []

    def held(lease: worker.Lease, root: Path) -> worker.Finished:
        seen.append(root)
        report = root / "report.json"
        report.write_text('{"key": "a"}')
        log = root / "log.txt"
        log.write_text("log of h1")
        rules = root / "rules"
        tests = rules / "tests"
        tests.mkdir(parents=True)
        (rules / "features.json").write_text("{}")
        (tests / "test_alpha.py").write_text("x")
        tests.chmod(0o555)
        rules.chmod(0o555)
        return worker.Finished(outcome="passed", report=report, log=log)

    runs = tmp_path / "runs"
    runs.mkdir()
    one = worker.Worker(worker.Client(scheduler), {"test": held}, "pod-1", 1, runs, wait_ms=0)

    try:
        one.run_once()
        assert one.drain() == []
        [root] = seen
        assert not root.exists()
        assert sink.bodies == {"/h1:test:a/report": b'{"key": "a"}', "/h1:test:a/log": b"log of h1"}
    finally:
        for root in seen:
            if (root / "rules").exists():
                (root / "rules" / "tests").chmod(0o700)
                (root / "rules").chmod(0o700)


@pytest.mark.feature("lab-worker-run-directory-reclaimed")
def test_a_unit_whose_lease_root_cannot_be_cut_heartbeats_nothing_and_its_lease_reaps(
    scheduler: str, lab: Counted, jobs: queue.Queue, clock: Clock, sink: Sink, tmp_path: Path
) -> None:
    jobs.enqueue([UNIT_A], "h1", {})
    ran: list[Path] = []

    def held(lease: worker.Lease, root: Path) -> worker.Finished:
        ran.append(root)
        return worker.Finished(outcome="passed", report=root / "report.json", log=root / "log.txt")

    missing = tmp_path / "runs"
    rootless = worker.Worker(worker.Client(scheduler), {"test": held}, "pod-1", 1, missing, wait_ms=0, heartbeat_s=0.01)

    rootless.run_once()

    assert rootless.drain() == []
    time.sleep(0.5)
    assert ran == []
    assert lab.beats == []
    assert sink.bodies == {}
    clock.now += lab.ttl_ms + 1
    assert lab.reap(clock.now) == [("h1:test:a", "pending")]


@pytest.mark.feature("lab-worker-run-directory-reclaimed")
def test_a_unit_whose_lease_root_resists_removal_is_delivered_and_reports_no_crash(
    scheduler: str, jobs: queue.Queue, sink: Sink, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jobs.enqueue([UNIT_A], "h1", {})
    seen: list[Path] = []

    def held(lease: worker.Lease, root: Path) -> worker.Finished:
        seen.append(root)
        root.rmdir()
        root.write_text("")
        return finished(tmp_path, lease)

    runs = tmp_path / "runs"
    runs.mkdir()
    one = worker.Worker(worker.Client(scheduler), {"test": held}, "pod-1", 1, runs, wait_ms=0)

    one.run_once()

    assert one.drain() == []
    [root] = seen
    assert root.is_file()
    assert sink.bodies == {"/h1:test:a/report": b'{"key": "a"}', "/h1:test:a/log": b"log of h1"}
    assert found(jobs, "h1:test:a").outcome == "passed"
    assert "crashed" not in capsys.readouterr().err
