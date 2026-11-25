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


@pytest.mark.feature("queue-watch-job-states")
def test_a_running_or_completed_shard_job_says_the_scope_has_started(
    jobs_api: tuple[str, list[dict[str, object]], list[tuple[str, str]]],
) -> None:
    api, jobs, calls = jobs_api
    jobs.extend(
        [
            {"name": "unit (3.12, 1)", "status": "in_progress"},
            {"name": "unit (3.12, 2)", "status": "in_progress"},
        ]
    )
    assert queue_watch.started(api, "o/r", "9", "1", "token-x", "unit (") is True
    path, authorization = calls[0]
    assert path == "/repos/o/r/actions/runs/9/attempts/1/jobs?per_page=100"
    assert authorization == "Bearer token-x"


@pytest.mark.feature("queue-watch-job-states")
@pytest.mark.parametrize("prefix", ["unit (", "unit-selected ("])
@pytest.mark.parametrize(
    "statuses",
    [("queued", "in_progress"), ("in_progress", "queued")],
    ids=["queued-first", "running-first"],
)
def test_a_queued_shard_among_running_ones_reports_not_started(
    jobs_api: tuple[str, list[dict[str, object]], list[tuple[str, str]]],
    prefix: str,
    statuses: tuple[str, str],
) -> None:
    api, jobs, _ = jobs_api
    jobs.extend(
        [
            {"name": f"{prefix}3.12, 1)", "status": statuses[0]},
            {"name": f"{prefix}3.12, 2)", "status": statuses[1]},
        ]
    )
    assert queue_watch.started(api, "o/r", "9", "1", "token-x", prefix) is False


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


@pytest.mark.feature("queue-watch-job-states")
def test_a_selected_legs_job_name_is_matched_with_its_own_prefix(
    jobs_api: tuple[str, list[dict[str, object]], list[tuple[str, str]]],
) -> None:
    api, jobs, _ = jobs_api
    jobs.append({"name": "unit-selected (3.12, 1)", "status": "in_progress"})
    assert queue_watch.started(api, "o/r", "9", "1", "token-x", "unit-selected (") is True


@pytest.mark.feature("queue-watch-job-states")
def test_no_matching_shard_job_name_reads_as_unknown(
    jobs_api: tuple[str, list[dict[str, object]], list[tuple[str, str]]],
) -> None:
    api, jobs, _ = jobs_api
    jobs.append({"name": "lint", "status": "in_progress"})
    assert queue_watch.started(api, "o/r", "9", "1", "token-x", "unit (") is None


@pytest.mark.feature("queue-watch-job-states")
def test_an_unreachable_jobs_api_reads_as_unknown() -> None:
    assert queue_watch.started("http://127.0.0.1:9", "o/r", "9", "1", "token-x", "unit (") is None


@pytest.mark.feature("queue-watch-main-state-file")
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


@pytest.mark.feature("queue-watch-main-state-file")
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


@pytest.mark.feature("dep-manifest-raw-arrivals")
@pytest.mark.parametrize(
    ("history", "complete"),
    [
        ([], False),
        ([("unit (3.12, 1)", 1, "queued"), ("unit (3.12, 2)", 1, "queued")], False),
        ([("unit (3.12, 1)", 1, "success"), ("unit (3.12, 2)", 1, "success")], True),
        ([("unit (3.12, 1)", 1, "success"), ("unit (3.12, 2)", 1, "failure"), ("unit (3.12, 2)", 2, "success")], True),
        (
            [("unit (3.12, 1)", 1, "success"), ("unit (3.12, 2)", 1, "success"), ("dep-manifest", 2, "in_progress")],
            True,
        ),
        ([("unit (3.12, 1)", 1, "success"), ("unit (3.12, 2)", 1, "success"), ("select", 2, "success")], False),
        (
            [
                ("unit (3.12, 1)", 1, "success"),
                ("unit (3.12, 2)", 1, "success"),
                ("select", 2, "success"),
                ("unit (3.12, 1)", 2, "success"),
                ("unit (3.12, 2)", 2, "success"),
            ],
            True,
        ),
        ([("unit (3.12, 1)", 2, "success"), ("unit (3.12, 2)", 2, "failure")], False),
        ([("unit (3.12, 1)", 2, "success")], False),
    ],
    ids=[
        "empty",
        "queued",
        "success",
        "partial-rerun",
        "manifest-only",
        "matrix-not-created",
        "matrix-created",
        "failed-producer",
        "missing-shard",
    ],
)
def test_raw_watch_requires_a_complete_successful_final_matrix(
    history: list[tuple[str, int, str]],
    complete: bool,
    jobs_api: tuple[str, list[dict[str, object]], list[tuple[str, str]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api, jobs, calls = jobs_api
    for ident, (name, attempt, result) in enumerate(history, 1):
        jobs.append(
            {
                "id": ident,
                "name": name,
                "run_attempt": attempt,
                "status": result if result in {"queued", "in_progress"} else "completed",
                "conclusion": result if result in {"success", "failure"} else None,
            }
        )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GH_APP_TOKEN", "token-x")
    monkeypatch.setenv("GITHUB_API_URL", api)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "9")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("COVERAGE_RAW", "true")
    monkeypatch.setenv("SHARDS", "2")
    monkeypatch.setattr(queue_watch, "INTERVAL", 0)
    monkeypatch.setattr(queue_watch, "BOUND", 0.03)
    monkeypatch.setattr("sys.argv", ["queue_watch.py", "unit ("])
    assert queue_watch.main() == 0
    state = json.loads((tmp_path / f"{queue_watch.STATE}.complete").read_text())
    assert state == {"run": "9", "attempt": 2, "complete": complete}
    assert any("filter=all" in path for path, _ in calls)
