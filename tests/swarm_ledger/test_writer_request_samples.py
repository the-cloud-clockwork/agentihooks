"""Each ledger write the host warm writer or the in process fallback answers keeps one request metric sample, spooled
beside the ledger server's journal and returned once by the server's metrics reader, in this process and across
processes."""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.request import urlopen

import pytest

from hooks import config
from scripts.swarm_ledger import ledger_server, request_metrics, warm_writer
from scripts.tick.adapters import database_ledger, writer_client
from tests.swarm_ledger import test_request_metrics_journal, test_warm_writer
from tests.swarm_ledger.test_request_metrics_journal import fresh
from tests.swarm_ledger.test_request_metrics_steps import Clock
from tests.swarm_ledger.test_warm_writer import COMMENT, STALE, Twins, guarded, in_process, over_socket, raw, state

metrics_url = test_request_metrics_journal.metrics_url
paced = test_warm_writer.paced
twins = test_warm_writer.twins
writer = test_warm_writer.writer
ROOT = Path(__file__).resolve().parents[2]
WALL = 2.5
CPU = 0.5
OPERATIONS = "/api/v1/ledgers/:slug/operations"
WRITTEN = f"{OPERATIONS} add"
TASKS = "/api/v1/ledgers/:slug/tasks"
TASKS_PATH = "/api/v1/ledgers/spooled/tasks"
CAPACITY = 128
SERVED = 30
PER_CHILD = 35
CHILD = (
    "import sys, time\n"
    "from scripts.swarm_ledger import request_metrics\n"
    "for _ in range(int(sys.argv[1])):\n"
    f"    with request_metrics.command('POST', {TASKS_PATH!r}):\n"
    "        time.sleep(0.005)\n"
)


def applied(slug: str, turn: int) -> dict[str, Any]:
    """A comment write the ledger applies, its operation and comment named by ``turn``."""
    comment = {**COMMENT, "id": f"sample-comment-{turn}"}
    return {"operation_id": f"sample-op-{turn}", "ops": [comment], "guards": guarded(slug, "phases/p1/comments")}


def refused(slug: str, turn: int) -> dict[str, Any]:
    """A comment write the ledger refuses on a stale revision guard."""
    return {"operation_id": f"sample-op-{turn}", "ops": [COMMENT], "guards": {"phases/p1/comments": STALE}}


PAYLOADS = [pytest.param(applied, 200, id="applied"), pytest.param(refused, 409, id="refused")]


@pytest.fixture
def folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ledger folder of this test's own, where writers spool their samples and the owner keeps its journal."""
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def owner(folder: Path) -> Path:
    """This process as the ledger server keeping the journal in ``folder``."""
    journal = folder / request_metrics.JOURNAL_NAME
    request_metrics.restore(journal)
    return journal


@pytest.fixture
def ticking(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Request metric clocks that move only while an in process answer runs, by ``WALL`` and ``CPU`` seconds."""
    clock = Clock()
    real = database_ledger.replied

    def replied(*args: Any) -> Any:
        clock.wall += WALL
        clock.cpu += CPU
        return real(*args)

    monkeypatch.setattr(request_metrics, "time", clock)
    monkeypatch.setattr(database_ledger, "replied", replied)
    return clock


@pytest.fixture
def home(memory_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An agentihooks home with no writer socket, so a command line write takes the in process fallback."""
    monkeypatch.setattr(config, "AGENTIHOOKS_HOME", memory_path)
    monkeypatch.chdir(memory_path)
    return memory_path


def held() -> list[tuple[str, str, float, float]]:
    """The method, route, wall and CPU of each write sample the owner's report holds after one journal pass."""
    request_metrics.persist()
    found = request_metrics.records(1.0, None, request_metrics.MAX_RECORDS)
    return [
        (route["method"], route["route"], record["wall_seconds"], record["cpu_seconds"])
        for route in found["routes"]
        for record in route["records"]
    ]


def written(count: int) -> list[tuple[str, str, float, float]]:
    """The write samples the owner's report holds once its passes took ``count`` of them, waiting at most ten
    seconds, read again after one more pass."""
    deadline = time.monotonic() + 10.0
    while len(held()) < count and time.monotonic() < deadline:
        time.sleep(0.01)
    return held()


def fallback(slug: str, payload: dict[str, Any]) -> tuple[int, Any]:
    """The status and decoded body a command line write gets through the in process fallback."""
    try:
        return 200, writer_client.routed(5.0, slug, "operations", payload)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@contextmanager
def refusing(path: Path) -> Iterator[None]:
    """A spool lock that cannot be taken."""
    raise OSError("lock refused")
    yield


def children(count: int, folder: Path) -> list[subprocess.Popen[bytes]]:
    """``count`` fresh interpreters each answering ``PER_CHILD`` command line writes that spool into ``folder``."""
    env = {key: value for key, value in os.environ.items() if not key.startswith(("COV_CORE_", "COVERAGE_"))}
    env["LEDGER_DIR"] = str(folder)
    root = ROOT.parent if ROOT.name == "mutants" else ROOT
    return [subprocess.Popen([sys.executable, "-c", CHILD, str(PER_CHILD)], cwd=root, env=env) for _ in range(count)]


def counted(found: dict[str, Any]) -> dict[str, int]:
    """Each write route a metrics read names, with its sample count."""
    return {route["route"]: route["count"] for route in found["routes"]}


@pytest.mark.feature("ledger-writer-request-samples")
@pytest.mark.parametrize(("payload", "status"), PAYLOADS)
def test_each_socket_write_keeps_one_post_sample_with_its_wall_in_the_server_report(
    twins: Twins,
    writer: Path,
    owner: Path,
    ticking: Clock,
    payload: Callable[[str, int], dict[str, Any]],
    status: int,
) -> None:
    for turn in (1, 2):
        with database_ledger.acting(""):
            expected = in_process(twins.there, "operations", payload(twins.there, turn))
            answered = over_socket(writer, twins.here, "operations", payload(twins.here, turn))
        assert answered == expected
        assert answered[0] == status
    assert state(twins.here) == state(twins.there)
    assert written(2) == [("POST", WRITTEN, WALL, CPU)] * 2


@pytest.mark.feature("ledger-writer-request-samples")
@pytest.mark.parametrize(("payload", "status"), PAYLOADS)
def test_each_fallback_write_keeps_one_post_sample_with_its_wall_in_the_server_report(
    twins: Twins,
    home: Path,
    owner: Path,
    ticking: Clock,
    payload: Callable[[str, int], dict[str, Any]],
    status: int,
) -> None:
    for turn in (1, 2):
        with database_ledger.acting(""):
            expected = in_process(twins.there, "operations", payload(twins.there, turn))
            answered = fallback(twins.here, payload(twins.here, turn))
        assert answered == (expected[0], json.loads(expected[1]))
        assert answered[0] == status
    assert state(twins.here) == state(twins.there)
    assert written(2) == [("POST", WRITTEN, WALL, CPU)] * 2


@pytest.mark.feature("ledger-writer-request-samples")
def test_a_socket_read_and_an_empty_probe_keep_no_sample(
    twins: Twins, writer: Path, owner: Path, ticking: Clock
) -> None:
    with database_ledger.acting(""):
        assert over_socket(writer, twins.here, "tasks")[0] == 200
        assert raw(writer, b"") == b""
        assert over_socket(writer, twins.here, "operations", applied(twins.here, 1))[0] == 200
    assert written(1) == [("POST", WRITTEN, WALL, CPU)]


@pytest.mark.feature("ledger-writer-request-samples")
def test_a_fallback_write_inside_a_server_request_keeps_only_the_server_sample(
    twins: Twins, owner: Path, ticking: Clock
) -> None:
    def handle_one_request(handler: Any) -> None:
        database_ledger.request(twins.here, "operations", applied(twins.here, 1))

    handler = SimpleNamespace(raw_requestline=b"POST", command="POST", path=f"/api/v1/ledgers/{twins.here}/operations")
    with database_ledger.acting(""):
        request_metrics.measured(handle_one_request)(handler)
    assert written(1) == [("POST", WRITTEN, WALL, CPU)]


@pytest.mark.feature("ledger-writer-request-samples")
def test_a_socket_write_caller_has_its_reply_while_the_writer_still_keeps_the_sample(
    twins: Twins, writer: Path, owner: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keeping, release = threading.Event(), threading.Event()
    real = request_metrics.exclusive

    @contextmanager
    def held_back(path: Path) -> Iterator[None]:
        keeping.set()
        release.wait(10.0)
        with real(path):
            yield

    monkeypatch.setattr(request_metrics, "exclusive", held_back)
    with database_ledger.acting(""):
        sock = warm_writer.connected(2.0, writer)
        status, _ = writer_client.ask(sock, twins.here, "operations", applied(twins.here, 1))
    assert status == 200
    assert keeping.wait(10.0)
    release.set()
    assert [(method, route) for method, route, _, _ in written(1)] == [("POST", WRITTEN)]


@pytest.mark.feature("ledger-writer-request-samples")
def test_an_idle_owner_pass_leaves_its_journal_bytes_unchanged(owner: Path) -> None:
    request_metrics.record("POST", OPERATIONS, 1.0, 0.0, 0.0)
    request_metrics.persist()
    before = owner.read_bytes()
    request_metrics.persist()
    assert owner.read_bytes() == before


@pytest.mark.feature("ledger-writer-request-samples")
def test_an_owner_returns_each_sample_child_writers_spool_during_its_rewrites_once_through_its_metrics_reader(
    folder: Path, owner: Path, metrics_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(request_metrics, "SAMPLES", deque(maxlen=CAPACITY))
    rewrites: list[Path] = []
    real = request_metrics.replace_lines

    def rewritten(path: Path, lines: list) -> None:
        rewrites.append(path)
        real(path, lines)

    monkeypatch.setattr(request_metrics, "replace_lines", rewritten)
    running = children(2, folder)
    served = 0
    while any(child.poll() is None for child in running) or served < SERVED:
        if served < SERVED:
            request_metrics.record("POST", OPERATIONS, 1.0, 0.0, 0.0)
            served += 1
        request_metrics.persist()
        time.sleep(0.01)
    assert [child.wait(60) for child in running] == [0, 0]
    request_metrics.persist()
    with urlopen(f"{metrics_url}/api/v1/metrics?view=writes&hours=1&limit=1000") as response:
        found = json.load(response)
    assert counted(found) == {OPERATIONS: SERVED, TASKS: 2 * PER_CHILD}
    assert found["matched"] == SERVED + 2 * PER_CHILD
    assert rewrites != []
    fresh(monkeypatch, request_metrics.STARTED_AT)
    monkeypatch.setattr(request_metrics, "SAMPLES", deque(maxlen=CAPACITY))
    request_metrics.restore(owner)
    assert counted(request_metrics.records(1.0, None, 1000)) == {OPERATIONS: SERVED, TASKS: 2 * PER_CHILD}


@pytest.mark.feature("ledger-writer-request-samples")
def test_a_legacy_journal_stays_readable_beside_spooled_samples(folder: Path) -> None:
    journal = folder / request_metrics.JOURNAL_NAME
    now = time.time()
    journal.write_text(
        f'["s", {now - 60.0}, "POST", "{OPERATIONS}", 1.0, 0.0, 0.0]\n'
        f'["s", {now - 30.0}, "POST", "{OPERATIONS}", 2.0, 0.0, 0.0, 0.0, 0.0]\n'
    )
    request_metrics.restore(journal)
    with request_metrics.command("POST", TASKS_PATH):
        pass
    request_metrics.persist()
    found = request_metrics.records(1.0, None, 10)
    assert counted(found) == {OPERATIONS: 2, TASKS: 1}
    assert [record["wall_seconds"] for record in found["routes"][0]["records"]] == [1.0, 2.0]


def spooled(folder: Path) -> tuple[list[float], int]:
    """The wall of each sample spooled in ``folder``, oldest first, and the spool's size in bytes."""
    spool = (folder / request_metrics.JOURNAL_NAME).with_suffix(request_metrics.SPOOL_SUFFIX)
    return [row[4] for row in request_metrics.journal_rows(spool)], spool.stat().st_size


@pytest.mark.feature("ledger-writer-request-samples")
def test_a_spool_no_server_takes_keeps_only_its_newest_samples_under_its_cap(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    monkeypatch.setattr(request_metrics, "time", clock)
    monkeypatch.setattr(request_metrics, "SPOOL_BYTES", 1000)
    monkeypatch.setattr(request_metrics, "MAX_SAMPLES", 4)
    for turn in range(1, 31):
        with request_metrics.command("POST", TASKS_PATH):
            clock.wall += float(turn)
    walls, size = spooled(folder)
    assert size <= request_metrics.SPOOL_BYTES + 200
    assert 4 < len(walls) <= 20
    assert walls == [float(turn) for turn in range(31 - len(walls), 31)]


@pytest.mark.feature("ledger-writer-request-samples")
def test_a_spool_no_server_takes_drops_samples_past_retention_when_it_compacts(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    monkeypatch.setattr(request_metrics, "time", clock)
    monkeypatch.setattr(request_metrics, "SPOOL_BYTES", 1000)
    for turn in range(1, 21):
        if turn == 11:
            clock.wall += request_metrics.RETENTION_SECONDS + 1.0
        with request_metrics.command("POST", TASKS_PATH):
            clock.wall += float(turn)
    assert spooled(folder)[0] == [float(turn) for turn in range(11, 21)]


@pytest.mark.feature("ledger-writer-request-samples")
def test_a_spool_the_owner_cannot_take_never_stops_its_own_journal_write(
    folder: Path, owner: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with request_metrics.command("POST", TASKS_PATH):
        pass
    request_metrics.record("POST", OPERATIONS, 1.0, 0.0, 0.0)
    real = request_metrics.exclusive
    monkeypatch.setattr(request_metrics, "exclusive", refusing)
    ledger_server.metrics_journal(request_metrics.persist)
    assert capsys.readouterr().err == "request metrics spool: lock refused\n"
    rows = [json.loads(line) for line in owner.read_text().splitlines()]
    assert [(row[0], row[3]) for row in rows if row[0] == "s"] == [("s", OPERATIONS)]
    monkeypatch.setattr(request_metrics, "exclusive", real)
    assert counted(request_metrics.records(1.0, None, 10)) == {OPERATIONS: 1}
    request_metrics.persist()
    assert counted(request_metrics.records(1.0, None, 10)) == {OPERATIONS: 1, TASKS: 1}


@pytest.mark.feature("ledger-writer-request-samples")
def test_a_write_whose_sample_cannot_be_spooled_still_answers_and_names_the_failure(
    folder: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(request_metrics, "exclusive", refusing)
    answered = []
    with request_metrics.command("POST", TASKS_PATH):
        answered.append(True)
    assert answered == [True]
    assert capsys.readouterr().err == "request metrics spool: lock refused\n"


@pytest.mark.feature("ledger-writer-request-samples")
def test_a_host_with_no_ledger_folder_spools_nothing_and_says_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path / "none"))
    with request_metrics.command("POST", TASKS_PATH):
        pass
    assert list(tmp_path.iterdir()) == []
    assert capsys.readouterr().err == ""
