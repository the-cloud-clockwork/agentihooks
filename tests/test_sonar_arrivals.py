import base64
import importlib.util
import io
import json
import threading
import time
import zipfile
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
UPLOADER = {"workflow_run_backend_id": "run-1", "workflow_job_run_backend_id": "job-9"}


def _arrivals() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sonar_arrivals", ROOT / ".github/coverage/arrivals.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


arrivals = _arrivals()


class Service:
    """Answer each listing from a script of rounds, the last one repeating, and unpack named files on fetch."""

    def __init__(self, rounds: list[dict[str, str] | None], refused: frozenset[str] = frozenset()) -> None:
        self.rounds = rounds
        self.refused = refused
        self.fetched: list[str] = []
        self.listings = 0

    def listed(self) -> dict[str, str] | None:
        round_ = self.rounds[min(self.listings, len(self.rounds) - 1)]
        self.listings += 1
        return round_

    def fetch(self, name: str, folder: Path) -> bool:
        if name in self.refused:
            return False
        self.fetched.append(name)
        folder.mkdir(parents=True)
        (folder / ".coverage").write_text(name)
        return True


class Clock:
    """A clock that moves only when the wait pauses."""

    def __init__(self) -> None:
        self.moment = 0.0
        self.pauses: list[float] = []

    def now(self) -> float:
        return self.moment

    def pause(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.moment += seconds


class RealTime(Clock):
    """A moving clock whose pauses also give running processes a moment of real time."""

    def pause(self, seconds: float) -> None:
        super().pause(seconds)
        time.sleep(0.01)


def _arrive(
    tmp_path: Path, service: Service, shards: int, scope: str = "full", attempt: int = 1, seconds: float = 600
) -> tuple[list[int], Clock]:
    clock = Clock()
    left = arrivals.arrive(service, shards, scope, attempt, tmp_path / "shards", seconds, clock)
    return left, clock


def test_each_shards_coverage_is_taken_as_it_lands_and_the_wait_ends_at_the_last(tmp_path: Path) -> None:
    service = Service([{}, {"coverage-3.12-2": "7"}, {"coverage-3.12-2": "7", "coverage-3.12-1": "9"}])
    left, clock = _arrive(tmp_path, service, 2)
    assert left == []
    assert service.fetched == ["coverage-3.12-2", "coverage-3.12-1"]
    assert service.listings == 3
    assert clock.pauses == [arrivals.INTERVAL, arrivals.INTERVAL]
    assert (tmp_path / "shards/coverage-3.12-1/.coverage").read_text() == "coverage-3.12-1"


def test_a_shard_whose_flake_report_lands_without_coverage_has_ended(tmp_path: Path) -> None:
    service = Service([{"coverage-3.12-1": "1", "flakes-3.12-1": "2", "flakes-3.12-2": "3"}])
    left, _ = _arrive(tmp_path, service, 2)
    assert left == []
    assert service.fetched == ["coverage-3.12-1"]


def test_a_listed_coverage_that_did_not_download_keeps_its_shard_waiting(tmp_path: Path) -> None:
    listing = {"coverage-3.12-1": "1", "flakes-3.12-1": "2"}
    service = Service([listing], refused=frozenset({"coverage-3.12-1"}))
    left, clock = _arrive(tmp_path, service, 1, seconds=3 * arrivals.INTERVAL)
    assert left == [1]
    assert clock.pauses == [arrivals.INTERVAL] * 3


def test_a_rerun_waits_past_the_flake_report_an_earlier_attempt_left(tmp_path: Path) -> None:
    service = Service([{"flakes-3.12-1": "5"}, {"flakes-3.12-1": "5"}, {"flakes-3.12-1": "8"}])
    left, _ = _arrive(tmp_path, service, 1, attempt=2)
    assert left == []
    assert service.listings == 3


def test_a_rerun_ends_a_shard_whose_first_flake_report_lands_in_it(tmp_path: Path) -> None:
    service = Service([{}, {"flakes-3.12-1": "4"}])
    left, _ = _arrive(tmp_path, service, 1, attempt=3)
    assert left == []
    assert service.listings == 2


def test_a_rerun_takes_coverage_an_earlier_attempt_left(tmp_path: Path) -> None:
    service = Service([{"coverage-3.12-1": "3", "flakes-3.12-1": "4"}])
    left, _ = _arrive(tmp_path, service, 1, attempt=2)
    assert left == []
    assert service.fetched == ["coverage-3.12-1"]


def test_the_wait_returns_the_shards_still_running_when_it_runs_out(tmp_path: Path) -> None:
    service = Service([{"coverage-3.12-2": "1"}])
    left, clock = _arrive(tmp_path, service, 3, seconds=2.5 * arrivals.INTERVAL)
    assert left == [1, 3]
    assert clock.moment == 3 * arrivals.INTERVAL


def test_an_unanswered_listing_is_asked_again(tmp_path: Path) -> None:
    service = Service([None, {"coverage-3.12-1": "1"}])
    left, _ = _arrive(tmp_path, service, 1)
    assert left == []
    assert service.listings == 2


def test_a_selected_scope_takes_each_legs_coverage_and_branch_coverage(tmp_path: Path) -> None:
    listing = {
        "branch-coverage-3.12-1-1": "1",
        "branch-coverage-3.12-1-2": "2",
        "branch-record-3.12-1-1": "3",
        "coverage-3.12-2": "4",
        "coverage-3.12-3": "5",
        "durations-3.12-2": "6",
    }
    service = Service([listing])
    left, _ = _arrive(tmp_path, service, 2, scope="selected")
    assert left == []
    assert service.fetched == ["branch-coverage-3.12-1-1", "branch-coverage-3.12-1-2", "coverage-3.12-2"]


def test_a_full_scope_waits_for_coverage_and_never_takes_branch_coverage(tmp_path: Path) -> None:
    service = Service([{"branch-coverage-3.12-1-1": "1"}])
    left, _ = _arrive(tmp_path, service, 1, seconds=arrivals.INTERVAL)
    assert left == [1]
    assert service.fetched == []


@pytest.mark.parametrize(
    ("name", "scope", "shard"),
    [
        ("coverage-3.12-12", "full", 12),
        ("coverage-3.12-13", "full", None),
        ("coverage-3.12-0", "full", None),
        ("branch-coverage-3.12-4-2", "full", None),
        ("branch-coverage-3.12-4-2", "selected", 4),
        ("branch-coverage-3.12-4", "selected", None),
        ("coverage-3.12-4-2", "selected", None),
        ("coverage-3.13-4", "selected", None),
        ("flakes-3.12-4", "selected", None),
    ],
)
def test_only_this_scopes_coverage_names_belong_to_a_shard(name: str, scope: str, shard: int | None) -> None:
    assert arrivals.shard_of(name, scope, 12) == shard


class Landing(Service):
    """List the early round until every awaited file exists, then the final round."""

    def __init__(self, early: dict[str, str], final: dict[str, str], awaited: list[Path]) -> None:
        super().__init__([early, final])
        self.awaited = awaited
        self.give_up = time.monotonic() + 30

    def listed(self) -> dict[str, str] | None:
        self.listings += 1
        landed = all(path.exists() for path in self.awaited) or time.monotonic() > self.give_up
        return self.rounds[1] if landed else self.rounds[0]


def _stub_flatten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked: bool = False) -> None:
    stub = tmp_path / "flatten.py"
    hold = "Path(sys.argv[2]).write_text('partial')\nwhile True: time.sleep(0.01)\n" if blocked else ""
    stub.write_text(
        "import shutil, sys, time\nfrom pathlib import Path\n" + hold + "shutil.copyfile(sys.argv[1], sys.argv[2])\n"
    )
    monkeypatch.setattr(arrivals, "FLATTEN", stub)


def _arrive_in_real_time(tmp_path: Path, service: Service, shards: int) -> list[int]:
    return arrivals.arrive(service, shards, "full", 1, tmp_path / "shards", 600, RealTime())


def test_databases_it_holds_are_flattened_beside_them_while_shards_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_flatten(tmp_path, monkeypatch)
    carried = tmp_path / "shards/branch-5/1-2"
    carried.mkdir(parents=True)
    (carried / ".coverage").write_text("carried")
    (carried / ".coverage.empty").write_text("")
    first = tmp_path / "shards/coverage-3.12-1/flat-.coverage"
    service = Landing(
        {"coverage-3.12-1": "1"}, {"coverage-3.12-1": "1", "coverage-3.12-2": "2"}, [first, carried / "flat-.coverage"]
    )
    assert _arrive_in_real_time(tmp_path, service, 2) == []
    assert first.read_text() == "coverage-3.12-1"
    assert (carried / "flat-.coverage").read_text() == "carried"
    assert not (carried / "flat-.coverage.empty").exists()
    assert not (tmp_path / "shards/coverage-3.12-2/flat-.coverage").exists()
    assert not list((tmp_path / "shards").rglob("*.part"))


def test_a_flatten_still_running_when_the_last_shard_lands_leaves_no_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_flatten(tmp_path, monkeypatch, blocked=True)
    partial = tmp_path / "shards/coverage-3.12-1/flat-.coverage.part"
    service = Landing({"coverage-3.12-1": "1"}, {"coverage-3.12-1": "1", "coverage-3.12-2": "2"}, [partial])
    assert _arrive_in_real_time(tmp_path, service, 2) == []
    assert service.fetched == ["coverage-3.12-1", "coverage-3.12-2"]
    assert not list((tmp_path / "shards").rglob("flat-*"))


def _token(run: str, job: str) -> str:
    claims = {"scp": f"Actions.GenericRead:x Actions.Results:{run}:{job} Actions.UploadArtifacts:{run}:{job}"}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def test_backend_ids_come_from_the_results_scope_of_the_runtime_token() -> None:
    assert arrivals.backend_ids(_token("run-1", "job-2")) == {
        "workflow_run_backend_id": "run-1",
        "workflow_job_run_backend_id": "job-2",
    }


@pytest.fixture
def results() -> Iterator[tuple[str, list[tuple[str, str, dict[str, object]]]]]:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(".coverage", "database")
        bundle.writestr("reused-coverage/.coverage", "reused")
    calls: list[tuple[str, str, dict[str, object]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append((self.path, self.headers["Authorization"], body))
            method = self.path.rsplit("/", 1)[1]
            if method == "ListArtifacts":
                answer: dict[str, object] = {
                    "artifacts": [
                        {"name": "coverage-3.12-1", "database_id": "41", **UPLOADER},
                        {"name": "flakes-3.12-1", "database_id": "40", **UPLOADER},
                        {"name": "flakes-3.12-1", "database_id": "44", **UPLOADER},
                    ]
                }
            elif body == {**UPLOADER, "name": "coverage-3.12-1"}:
                answer = {"signed_url": f"http://{self.headers['Host']}/blob"}
            else:
                self.send_response(404)
                self.end_headers()
                return
            data = json.dumps(answer).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            data = archive.getvalue()
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
        yield f"http://127.0.0.1:{server.server_port}/", calls
    finally:
        server.shutdown()
        server.server_close()


def test_the_results_service_lists_each_name_once_at_its_newest_id(
    results: tuple[str, list[tuple[str, str, dict[str, object]]]],
) -> None:
    base, calls = results
    token = _token("run-1", "job-2")
    assert arrivals.Results(base, token).listed() == {"coverage-3.12-1": "41", "flakes-3.12-1": "44"}
    path, authorization, body = calls[0]
    assert path == "/twirp/github.actions.results.api.v1.ArtifactService/ListArtifacts"
    assert authorization == f"Bearer {token}"
    assert body == {"workflow_run_backend_id": "run-1", "workflow_job_run_backend_id": "job-2"}


def test_a_fetched_artifact_is_unpacked_into_its_own_folder(
    results: tuple[str, list[tuple[str, str, dict[str, object]]]], tmp_path: Path
) -> None:
    base, _ = results
    out = tmp_path / "artifacts"
    out.mkdir()
    folder = out / "coverage-3.12-1"
    service = arrivals.Results(base, _token("run-1", "job-2"))
    assert service.listed() is not None
    assert service.fetch("coverage-3.12-1", folder) is True
    assert (folder / ".coverage").read_text() == "database"
    assert (folder / "reused-coverage/.coverage").read_text() == "reused"
    assert sorted(path.name for path in out.iterdir()) == ["coverage-3.12-1"]


def test_a_download_is_signed_for_the_job_that_uploaded_the_artifact(
    results: tuple[str, list[tuple[str, str, dict[str, object]]]], tmp_path: Path
) -> None:
    base, calls = results
    service = arrivals.Results(base, _token("run-1", "job-2"))
    service.listed()
    service.fetch("coverage-3.12-1", tmp_path / "coverage-3.12-1")
    assert calls[1][0] == "/twirp/github.actions.results.api.v1.ArtifactService/GetSignedArtifactURL"
    assert calls[1][2] == {**UPLOADER, "name": "coverage-3.12-1"}


def test_an_artifact_the_service_does_not_sign_is_not_fetched(
    results: tuple[str, list[tuple[str, str, dict[str, object]]]], tmp_path: Path
) -> None:
    base, _ = results
    out = tmp_path / "artifacts"
    out.mkdir()
    assert arrivals.Results(base, _token("run-1", "job-2")).fetch("missing", out / "missing") is False
    assert list(out.iterdir()) == []


def test_an_unreachable_service_answers_no_listing() -> None:
    assert arrivals.Results("http://127.0.0.1:9/", _token("run-1", "job-2")).listed() is None


def test_the_wait_without_a_runtime_token_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ACTIONS_RUNTIME_TOKEN", raising=False)
    monkeypatch.setenv("ACTIONS_RESULTS_URL", "http://127.0.0.1:9/")
    assert arrivals.main() == 1
    assert "::error::No artifact service token reached the coverage wait" in capsys.readouterr().out


def test_the_wait_names_the_shards_it_gave_up_on(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("ACTIONS_RUNTIME_TOKEN", _token("run-1", "job-2"))
    monkeypatch.setenv("ACTIONS_RESULTS_URL", "http://127.0.0.1:9/")
    monkeypatch.setenv("SHARDS", "2")
    monkeypatch.setenv("SCOPE", "full")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("WAIT_MINUTES", "0")
    monkeypatch.setattr(arrivals, "OUT", tmp_path / "shards")
    assert arrivals.main() == 0
    assert "::warning::Shards 1, 2 had not ended after 0 minutes" in capsys.readouterr().out


@pytest.mark.feature("dev-push-sonar-waits-for-shards-that-queue-late")
def test_the_wait_lasts_the_minutes_it_is_given(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ACTIONS_RUNTIME_TOKEN", _token("run-1", "job-2"))
    monkeypatch.setenv("ACTIONS_RESULTS_URL", "http://127.0.0.1:9/")
    monkeypatch.setenv("SHARDS", "2")
    monkeypatch.setenv("SCOPE", "full")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("WAIT_MINUTES", "59")
    monkeypatch.setattr(arrivals, "OUT", tmp_path / "shards")
    waits: list[float] = []

    def arrive(
        service: object, shards: int, scope: str, attempt: int, out: Path, seconds: float, timer: object
    ) -> list[int]:
        waits.append(seconds)
        return []

    monkeypatch.setattr(arrivals, "arrive", arrive)
    assert arrivals.main() == 0
    assert waits == [59 * 60]
