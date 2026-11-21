import base64
import email.message
import hashlib
import http.client
import io
import json
import threading
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import ci_unit_copy

pytestmark = pytest.mark.unit

IDS = {"workflow_run_backend_id": "run-1", "workflow_job_run_backend_id": "job-2"}
KINDS = (("durations", 14), ("coverage", 7), ("flakes", 14), ("executed-lines", 7))


def _token() -> str:
    claims = json.dumps({"scp": "Actions.GenericRead:x Actions.Results:run-1:job-2"}).encode()
    return "h." + base64.urlsafe_b64encode(claims).decode().rstrip("=") + ".s"


def _queue(root: Path, shards: int, *, reused: bool = False, timed: bool = True) -> Path:
    queue = root / "queue"
    for shard in range(1, shards + 1):
        durations = queue / f"durations-3.12-{shard}"
        durations.mkdir(parents=True)
        (durations / "durations.json").write_text(json.dumps({f"tests/test_a.py::test_{shard}": shard}))
        (durations / "durations.sha256").write_text(f"sha {shard}\n")
        if timed:
            (durations / "elapsed.json").write_text(json.dumps({"seconds": shard}))
        coverage = queue / f"coverage-3.12-{shard}"
        (coverage / "js-coverage").mkdir(parents=True)
        (coverage / ".coverage").write_text(f"database {shard}")
        (coverage / "js-coverage" / "page.json").write_text(f"page {shard}")
        if reused:
            (coverage / "reused-coverage").mkdir()
            (coverage / "reused-coverage" / ".coverage").write_text(f"reused {shard}")
        flakes = queue / f"flakes-3.12-{shard}"
        flakes.mkdir()
        (flakes / "flakes.jsonl").write_text(json.dumps({"test": f"t{shard}"}) + "\n")
    return queue


def _mapped(shards: int) -> dict[str, int]:
    return {str(leg): (leg - 1) % shards + 1 for leg in range(1, 21)}


def _files(folder: Path) -> dict[str, str]:
    return {
        path.relative_to(folder).as_posix(): path.read_text() for path in sorted(folder.rglob("*")) if path.is_file()
    }


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
def test_every_push_leg_is_laid_out_from_the_queue_leg_it_maps_onto(tmp_path: Path) -> None:
    queue = _queue(tmp_path, 16, reused=True)
    out = tmp_path / "out"
    ci_unit_copy.layout(queue, _mapped(16), out)
    assert sorted(path.name for path in out.iterdir()) == sorted(
        f"{kind}-3.12-{leg}" for kind, _ in KINDS for leg in range(1, 21)
    )
    for leg in range(1, 17):
        for kind in ("durations", "coverage", "flakes"):
            assert _files(out / f"{kind}-3.12-{leg}") == _files(queue / f"{kind}-3.12-{leg}")
    for leg in range(17, 21):
        source = leg - 16
        assert _files(out / f"coverage-3.12-{leg}") == _files(queue / f"coverage-3.12-{source}")
        assert _files(out / f"durations-3.12-{leg}") == {
            "durations.json": "{}\n",
            "durations.sha256": f"sha {source}\n",
        }
        assert _files(out / f"flakes-3.12-{leg}") == {"flakes.jsonl": ""}
    for leg in range(1, 21):
        assert (out / f"executed-lines-3.12-{leg}").is_dir()
        assert _files(out / f"executed-lines-3.12-{leg}") == {}


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
def test_a_leg_above_the_queue_count_needs_no_elapsed_time_in_its_queue_leg(tmp_path: Path) -> None:
    out = tmp_path / "out"
    ci_unit_copy.layout(_queue(tmp_path, 4, timed=False), _mapped(4), out)
    assert _files(out / "durations-3.12-5") == {"durations.json": "{}\n", "durations.sha256": "sha 1\n"}


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
def test_a_missing_queue_leg_stops_the_layout(tmp_path: Path) -> None:
    queue = _queue(tmp_path, 3)
    (queue / "coverage-3.12-2" / ".coverage").unlink()
    (queue / "coverage-3.12-2" / "js-coverage" / "page.json").unlink()
    (queue / "coverage-3.12-2" / "js-coverage").rmdir()
    (queue / "coverage-3.12-2").rmdir()
    with pytest.raises(FileNotFoundError):
        ci_unit_copy.layout(queue, _mapped(3), tmp_path / "out")


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
def test_an_archive_holds_every_file_under_the_folder_by_its_path_inside_it(tmp_path: Path) -> None:
    folder = tmp_path / "coverage-3.12-1"
    (folder / "js-coverage").mkdir(parents=True)
    (folder / "empty").mkdir()
    (folder / ".coverage").write_text("database")
    (folder / "js-coverage" / "page.json").write_text("page")
    with zipfile.ZipFile(io.BytesIO(ci_unit_copy.archive(folder))) as zipped:
        assert zipped.namelist() == [".coverage", "js-coverage/page.json"]
        assert zipped.read(".coverage") == b"database"
        assert zipped.read("js-coverage/page.json") == b"page"
        assert {info.compress_type for info in zipped.infolist()} == {zipfile.ZIP_DEFLATED}


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
def test_a_folder_without_files_is_never_uploaded(tmp_path: Path) -> None:
    folder = tmp_path / "flakes-3.12-1"
    (folder / "empty").mkdir(parents=True)
    with pytest.raises(ValueError, match=r" holds no file to upload$"):
        ci_unit_copy.archive(folder)


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
@pytest.mark.parametrize(
    ("longest", "expected"),
    [
        (None, "2026-12-02T13:45:12Z"),
        ("", "2026-12-02T13:45:12Z"),
        ("90", "2026-12-02T13:45:12Z"),
        ("3", "2026-11-21T13:45:12Z"),
    ],
    ids=["unset", "empty", "longer", "shorter"],
)
def test_the_expiry_holds_the_retention_days_inside_the_repository_longest_retention(
    monkeypatch: pytest.MonkeyPatch, longest: str | None, expected: str
) -> None:
    if longest is None:
        monkeypatch.delenv("GITHUB_RETENTION_DAYS", raising=False)
    else:
        monkeypatch.setenv("GITHUB_RETENTION_DAYS", longest)
    assert ci_unit_copy.expiry(14, datetime(2026, 11, 18, 13, 45, 12, 123456, tzinfo=UTC)) == expected


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
def test_the_backend_ids_come_from_the_runtime_token_scope() -> None:
    assert ci_unit_copy.backend_ids(_token()) == IDS


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
@pytest.mark.parametrize(("read", "residue"), [("xy", 2), ("xyz", 3)], ids=["two-pads", "one-pad"])
def test_the_backend_ids_come_from_a_token_whose_payload_needs_padding(read: str, residue: int) -> None:
    claims = json.dumps({"scp": f"Actions.GenericRead:{read} Actions.Results:run-1:job-2"}).encode()
    payload = base64.urlsafe_b64encode(claims).decode().rstrip("=")
    assert len(payload) % 4 == residue
    assert ci_unit_copy.backend_ids(f"h.{payload}.s") == IDS


class _Answer:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "_Answer":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def _http(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://results.example/", code, "refused", email.message.Message(), None)


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
def test_a_service_call_posts_this_jobs_request_and_reads_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = []

    def urlopen(request: urllib.request.Request, timeout: float) -> _Answer:
        sent.append((request, timeout))
        return _Answer(b'{"ok": true}')

    monkeypatch.setattr(ci_unit_copy.urllib.request, "urlopen", urlopen)
    results = ci_unit_copy.Results("https://results.example/", _token())
    assert results.call("ListArtifacts", {"name_filter": "a"}) == {"ok": True}
    ((request, timeout),) = sent
    assert request.full_url == (
        "https://results.example/twirp/github.actions.results.api.v1.ArtifactService/ListArtifacts"
    )
    assert request.get_method() == "POST"
    assert json.loads(request.data) == {**IDS, "name_filter": "a"}
    assert request.get_header("Authorization") == f"Bearer {_token()}"
    assert request.get_header("Content-type") == "application/json"
    assert timeout == 120


def _requested(monkeypatch: pytest.MonkeyPatch, answer: bytes) -> list[tuple[str, dict[str, str], str]]:
    made: list[tuple[str, dict[str, str], str]] = []
    real = urllib.request.Request

    def request(url: str, data: bytes, headers: dict[str, str], method: str) -> urllib.request.Request:
        made.append((url, headers, method))
        return real(url, data=data, headers=headers, method=method)

    def urlopen(sent: urllib.request.Request, timeout: float) -> _Answer:
        return _Answer(answer)

    monkeypatch.setattr(ci_unit_copy.urllib.request, "Request", request)
    monkeypatch.setattr(ci_unit_copy.urllib.request, "urlopen", urlopen)
    return made


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
def test_a_service_call_hands_its_exact_headers_and_method_to_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    made = _requested(monkeypatch, b"{}")
    ci_unit_copy.Results("https://results.example/", _token()).call("ListArtifacts", {})
    assert made == [
        (
            "https://results.example/twirp/github.actions.results.api.v1.ArtifactService/ListArtifacts",
            {"Content-Type": "application/json", "Authorization": f"Bearer {_token()}"},
            "POST",
        )
    ]


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
def test_an_empty_service_answer_reads_as_an_empty_object(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ci_unit_copy.urllib.request, "urlopen", lambda request, timeout: _Answer(b""))
    assert ci_unit_copy.Results("https://results.example/", _token()).call("DeleteArtifact", {}) == {}


def _scripted(monkeypatch: pytest.MonkeyPatch, answers: list[object]) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []

    def call(self: ci_unit_copy.Results, method: str, body: dict) -> object:
        calls.append((method, body))
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(ci_unit_copy.Results, "call", call)
    return calls


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
def test_a_created_artifact_answers_its_upload_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ci_unit_copy, "expiry", lambda days, now: f"in {days} days")
    calls = _scripted(monkeypatch, [{"ok": True, "signed_upload_url": "https://blob/a"}])
    results = ci_unit_copy.Results("https://results.example/", _token())
    assert results.create("durations-3.12-1", 14) == "https://blob/a"
    assert calls == [("CreateArtifact", {"name": "durations-3.12-1", "version": 4, "expires_at": "in 14 days"})]


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
def test_an_artifact_expiry_counts_from_the_clock_in_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    zones: list[object] = []

    def now(zone: object) -> datetime:
        zones.append(zone)
        return datetime(2026, 11, 21, 12, tzinfo=UTC)

    def expiry(days: int, at: datetime) -> str:
        return f"{days} days from {at.isoformat()}"

    monkeypatch.setattr(ci_unit_copy, "datetime", SimpleNamespace(now=now))
    monkeypatch.setattr(ci_unit_copy, "expiry", expiry)
    calls = _scripted(monkeypatch, [{"ok": True, "signed_upload_url": "https://blob/a"}])
    ci_unit_copy.Results("https://results.example/", _token()).create("coverage-3.12-1", 7)
    assert zones == [UTC]
    assert calls == [
        (
            "CreateArtifact",
            {"name": "coverage-3.12-1", "version": 4, "expires_at": "7 days from 2026-11-21T12:00:00+00:00"},
        )
    ]


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
def test_a_name_an_earlier_attempt_left_unfinalized_is_freed_and_created_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ci_unit_copy, "expiry", lambda days, now: f"in {days} days")
    calls = _scripted(monkeypatch, [_http(409), {}, {}, {"ok": True, "signed_upload_url": "https://blob/b"}])
    results = ci_unit_copy.Results("https://results.example/", _token())
    assert results.create("flakes-3.12-2", 14) == "https://blob/b"
    request = {"name": "flakes-3.12-2", "version": 4, "expires_at": "in 14 days"}
    assert calls == [
        ("CreateArtifact", request),
        ("FinalizeArtifact", {"name": "flakes-3.12-2", "size": "0"}),
        ("DeleteArtifact", {"name": "flakes-3.12-2"}),
        ("CreateArtifact", request),
    ]


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
def test_a_create_refused_for_another_reason_raises_that_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    refusal = _http(500)
    calls = _scripted(monkeypatch, [refusal])
    with pytest.raises(urllib.error.HTTPError) as raised:
        ci_unit_copy.Results("https://results.example/", _token()).create("coverage-3.12-3", 7)
    assert raised.value is refusal
    assert [method for method, _ in calls] == ["CreateArtifact"]


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
def test_a_create_the_service_did_not_confirm_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _scripted(monkeypatch, [{"ok": False, "signed_upload_url": "https://blob/c"}])
    with pytest.raises(RuntimeError, match=r"^the results service did not create coverage-3\.12-3$"):
        ci_unit_copy.Results("https://results.example/", _token()).create("coverage-3.12-3", 7)


def _uploaded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, answers: list[object]) -> tuple[list, list, bytes]:
    folder = tmp_path / "flakes-3.12-4"
    folder.mkdir()
    (folder / "flakes.jsonl").write_text("{}\n")
    calls = _scripted(monkeypatch, answers)
    monkeypatch.setattr(ci_unit_copy.Results, "create", lambda self, name, days: f"https://blob/{name}?days={days}")
    puts = []

    def urlopen(request: urllib.request.Request, timeout: float) -> _Answer:
        puts.append((request, timeout))
        return _Answer(b"")

    monkeypatch.setattr(ci_unit_copy.urllib.request, "urlopen", urlopen)
    ci_unit_copy.Results("https://results.example/", _token()).upload("flakes-3.12-4", folder, 14)
    return calls, puts, ci_unit_copy.archive(folder)


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
def test_an_upload_replaces_the_name_puts_the_zip_and_finalizes_it_with_its_digest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    held = {"workflow_run_backend_id": "run-0", "workflow_job_run_backend_id": "job-9", "database_id": 7}
    calls, puts, data = _uploaded(monkeypatch, tmp_path, [{"artifacts": [held]}, {}, {"ok": True}])
    assert calls == [
        ("ListArtifacts", {"name_filter": "flakes-3.12-4"}),
        (
            "DeleteArtifact",
            {"workflow_run_backend_id": "run-0", "workflow_job_run_backend_id": "job-9", "name": "flakes-3.12-4"},
        ),
        (
            "FinalizeArtifact",
            {"name": "flakes-3.12-4", "size": str(len(data)), "hash": "sha256:" + hashlib.sha256(data).hexdigest()},
        ),
    ]
    ((request, timeout),) = puts
    assert request.full_url == "https://blob/flakes-3.12-4?days=14"
    assert request.get_method() == "PUT"
    assert request.data == data
    assert request.get_header("X-ms-blob-type") == "BlockBlob"
    assert request.get_header("Content-type") == "zip"
    assert timeout == 120


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
def test_a_blob_put_hands_its_exact_headers_and_method_to_the_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    made = _requested(monkeypatch, b"")
    _uploaded(monkeypatch, tmp_path, [{}, {"ok": True}])
    assert made == [
        ("https://blob/flakes-3.12-4?days=14", {"x-ms-blob-type": "BlockBlob", "Content-Type": "zip"}, "PUT")
    ]


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
def test_an_upload_of_a_free_name_deletes_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls, _, _ = _uploaded(monkeypatch, tmp_path, [{}, {"ok": True}])
    assert [method for method, _ in calls] == ["ListArtifacts", "FinalizeArtifact"]


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
def test_an_upload_the_service_did_not_finalize_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match=r"^the results service did not finalize flakes-3\.12-4$"):
        _uploaded(monkeypatch, tmp_path, [{}, {"ok": False}])


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
@pytest.mark.parametrize("failures", [0, 1, 2], ids=["first", "second", "last"])
def test_an_upload_is_retried_in_place_until_it_lands(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failures: int
) -> None:
    attempts, pauses = [], []

    def upload(self: ci_unit_copy.Results, name: str, folder: Path, days: int) -> None:
        attempts.append((name, folder, days))
        if len(attempts) <= failures:
            raise OSError("reset")

    monkeypatch.setattr(ci_unit_copy.Results, "upload", upload)
    results = ci_unit_copy.Results("https://results.example/", _token())
    ci_unit_copy.retried(results, "durations-3.12-1", Path("legs"), 14, pauses.append)
    assert attempts == [("durations-3.12-1", Path("legs"), 14)] * (failures + 1)
    assert pauses == [10] * failures
    assert capsys.readouterr().out == "".join(
        f"::warning title=Retried upload::Attempt {attempt} to upload durations-3.12-1 failed: reset; retrying.\n"
        for attempt in range(1, failures + 1)
    )


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
@pytest.mark.parametrize(
    "failure",
    [
        OSError("reset"),
        ValueError("json"),
        KeyError("signed_upload_url"),
        RuntimeError("refused"),
        http.client.IncompleteRead(b""),
    ],
    ids=["transport", "answer", "address", "service", "read"],
)
def test_an_upload_that_fails_every_attempt_raises_its_last_failure(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    attempts, pauses = [], []

    def upload(self: ci_unit_copy.Results, name: str, folder: Path, days: int) -> None:
        attempts.append(name)
        raise failure

    monkeypatch.setattr(ci_unit_copy.Results, "upload", upload)
    results = ci_unit_copy.Results("https://results.example/", _token())
    with pytest.raises(type(failure)):
        ci_unit_copy.retried(results, "coverage-3.12-2", Path("legs"), 7, pauses.append)
    assert attempts == ["coverage-3.12-2"] * 3
    assert pauses == [10, 10]


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
def test_every_leg_uploads_its_artifacts_in_the_unit_job_order_with_their_retention(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sent: list[tuple[str, Path, int, object]] = []
    receivers: list[ci_unit_copy.Results] = []
    guard = threading.Lock()

    def retried(
        results: ci_unit_copy.Results, name: str, folder: Path, days: int, pause: Callable[[float], None]
    ) -> None:
        with guard:
            sent.append((name, folder, days, pause))
            receivers.append(results)

    sizes = []
    pool = ci_unit_copy.ThreadPoolExecutor

    def sized(max_workers: int) -> ThreadPoolExecutor:
        sizes.append(max_workers)
        return pool(max_workers=max_workers)

    monkeypatch.setattr(ci_unit_copy, "retried", retried)
    monkeypatch.setattr(ci_unit_copy, "ThreadPoolExecutor", sized)
    pauses: list[float] = []
    results = ci_unit_copy.Results("https://results.example/", _token())
    ci_unit_copy.upload(tmp_path, results, pauses.append)
    assert sizes == [8]
    assert len(sent) == 80
    assert receivers == [results] * 80
    assert {entry[3] for entry in sent} == {pauses.append}
    for leg in range(1, 21):
        assert [entry[:3] for entry in sent if entry[0].endswith(f"-3.12-{leg}")] == [
            (f"{kind}-3.12-{leg}", tmp_path / f"{kind}-3.12-{leg}", days) for kind, days in KINDS
        ]


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
def test_the_layout_command_reads_the_queue_leg_map(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Path, dict[str, int], Path]] = []
    monkeypatch.setattr(ci_unit_copy, "layout", lambda queue, legs, out: seen.append((queue, legs, out)))
    assert ci_unit_copy.main(["layout", "--queue", "q", "--legs", '{"1": 1, "2": 1}', "--out", "o"]) == 0
    assert seen == [(Path("q"), {"1": 1, "2": 1}, Path("o"))]


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
def test_the_upload_command_reaches_the_results_service_with_the_runtime_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = []
    monkeypatch.setenv("ACTIONS_RESULTS_URL", "https://results.example/")
    monkeypatch.setenv("ACTIONS_RUNTIME_TOKEN", _token())
    monkeypatch.setattr(
        ci_unit_copy, "upload", lambda out, results: seen.append((out, results.base, results.token, results.ids))
    )
    assert ci_unit_copy.main(["upload", "--out", str(tmp_path)]) == 0
    assert seen == [(tmp_path, "https://results.example/", _token(), IDS)]


@pytest.mark.feature("dev-push-copies-reused-unit-results-in-one-job")
@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["layout", "--legs", "{}", "--out", "o"],
        ["layout", "--queue", "q", "--out", "o"],
        ["layout", "--queue", "q", "--legs", "{}"],
        ["upload"],
    ],
    ids=["command", "queue", "legs", "out", "upload-out"],
)
def test_each_command_requires_its_inputs(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        ci_unit_copy.main(argv)
    assert exited.value.code == 2
