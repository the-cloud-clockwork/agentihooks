import importlib.util
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def _queue_watch() -> ModuleType:
    spec = importlib.util.spec_from_file_location("queue_watch", ROOT / ".github/coverage/queue_watch.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


queue_watch = _queue_watch()


@pytest.mark.feature("queue-watch-job-states")
@pytest.mark.parametrize(
    ("job", "is_queued"),
    [
        ({"status": "queued"}, True),
        ({"status": "in_progress"}, False),
        ({"status": "completed"}, False),
        ({"status": "waiting"}, False),
    ],
)
def test_a_job_is_queued_only_when_its_status_is_queued(job: dict, is_queued: bool) -> None:
    assert queue_watch.queued(job) == is_queued


@pytest.fixture
def jobs_api() -> Iterator[tuple[str, list[dict[str, object]], list[tuple[str, str]]]]:
    jobs: list[dict[str, object]] = []
    calls: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            calls.append((self.path, self.headers["Authorization"]))
            data = json.dumps({"jobs": jobs}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{server.server_address[0]}:{server.server_port}", jobs, calls
    finally:
        server.shutdown()
        server.server_close()


def test_a_running_or_completed_shard_job_says_the_scope_has_started(
    jobs_api: tuple[str, list[dict[str, object]], list[tuple[str, str]]],
) -> None:
    api, jobs, calls = jobs_api
    jobs.extend(
        [
            {"name": "unit (3.12, 1)", "status": "queued"},
            {"name": "unit (3.12, 2)", "status": "in_progress"},
        ]
    )
    assert queue_watch.started(api, "o/r", "9", "1", "token-x", "unit (") is True
    path, authorization = calls[0]
    assert path == "/repos/o/r/actions/runs/9/attempts/1/jobs?per_page=100"
    assert authorization == "Bearer token-x"


@pytest.mark.feature("queue-watch-job-states")
def test_every_matching_shard_job_still_queued_says_the_scope_has_not_started(
    jobs_api: tuple[str, list[dict[str, object]], list[tuple[str, str]]],
) -> None:
    api, jobs, _ = jobs_api
    jobs.append({"name": "unit (3.12, 1)", "status": "queued"})
    assert queue_watch.started(api, "o/r", "9", "1", "token-x", "unit (") is False


@pytest.mark.feature("queue-watch-job-states")
def test_a_completed_shard_among_queued_ones_still_reports_not_started(
    jobs_api: tuple[str, list[dict[str, object]], list[tuple[str, str]]],
) -> None:
    api, jobs, _ = jobs_api
    jobs.extend(
        [
            {"name": "unit (3.12, 1)", "status": "completed"},
            {"name": "unit (3.12, 2)", "status": "queued"},
        ]
    )
    assert queue_watch.started(api, "o/r", "9", "1", "token-x", "unit (") is False


@pytest.mark.feature("queue-watch-job-states")
def test_every_matching_shard_job_completed_reports_started(
    jobs_api: tuple[str, list[dict[str, object]], list[tuple[str, str]]],
) -> None:
    api, jobs, _ = jobs_api
    jobs.append({"name": "unit (3.12, 1)", "status": "completed"})
    assert queue_watch.started(api, "o/r", "9", "1", "token-x", "unit (") is True


def test_a_selected_legs_job_name_is_matched_with_its_own_prefix(
    jobs_api: tuple[str, list[dict[str, object]], list[tuple[str, str]]],
) -> None:
    api, jobs, _ = jobs_api
    jobs.append({"name": "unit-selected (3.12, 1)", "status": "in_progress"})
    assert queue_watch.started(api, "o/r", "9", "1", "token-x", "unit-selected (") is True


def test_no_matching_shard_job_name_reads_as_unknown(
    jobs_api: tuple[str, list[dict[str, object]], list[tuple[str, str]]],
) -> None:
    api, jobs, _ = jobs_api
    jobs.append({"name": "lint", "status": "in_progress"})
    assert queue_watch.started(api, "o/r", "9", "1", "token-x", "unit (") is None


def test_an_unreachable_jobs_api_reads_as_unknown() -> None:
    assert queue_watch.started("http://127.0.0.1:9", "o/r", "9", "1", "token-x", "unit (") is None


def test_main_keeps_the_state_file_current_until_its_bound_runs_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GH_APP_TOKEN", "token-x")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "9")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setattr(queue_watch, "INTERVAL", 0)
    monkeypatch.setattr(queue_watch, "BOUND", 0.05)
    answers = iter([False, True])
    monkeypatch.setattr(queue_watch, "started", lambda *_args: next(answers, True))
    monkeypatch.setattr("sys.argv", ["queue_watch.py", "unit ("])
    assert queue_watch.main() == 0
    assert (tmp_path / queue_watch.STATE).read_text() in {"queued", "started"}


def test_an_unread_poll_leaves_the_state_file_as_it_was(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GH_APP_TOKEN", "token-x")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "9")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setattr(queue_watch, "INTERVAL", 0)
    monkeypatch.setattr(queue_watch, "BOUND", 0.05)
    monkeypatch.setattr(queue_watch, "started", lambda *_args: None)
    monkeypatch.setattr("sys.argv", ["queue_watch.py", "unit ("])
    assert queue_watch.main() == 0
    assert not (tmp_path / queue_watch.STATE).exists()
