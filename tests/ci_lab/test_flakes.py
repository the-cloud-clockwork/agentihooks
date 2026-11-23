import argparse
import io
import json
import os
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from scripts.ci_lab import evidence, flakes, objects, scheduler
from scripts.cli_delegates import delegated_cli
from scripts.swarm_v2.artifacts.object_store import PAGES
from tests import flake_budget

NOW_S = 1_800_000_000.0
WEEK_S = 168 * 3600
FIRST = f"test-v1-{'a' * 64}"
SECOND = f"test-v1-{'b' * 64}"
OLD = f"test-v1-{'c' * 64}"
SETTINGS = {
    "CI_LAB_MINIO_ENDPOINT": "http://localhost:9000",
    "CI_LAB_MINIO_BUCKET": "bucket",
    "CI_LAB_MINIO_ACCESS_KEY": "reader",
    "CI_LAB_MINIO_SECRET_KEY": "unused",
}
UNREACHABLE = 'Could not connect to the endpoint URL: "http://localhost:9000"'
DENIED = "An error occurred (AccessDenied) when calling the ListObjectsV2 operation: Access Denied"


class Bucket:
    def __init__(self, pages: list[dict[str, float]], bodies: dict[str, bytes]) -> None:
        self.pages = pages
        self.bodies = bodies
        self.requests: list[dict[str, str]] = []

    def list_objects_v2(self, **request: str) -> dict[str, Any]:
        self.requests.append(request)
        index = int(request.get("ContinuationToken", "0"))
        page: dict[str, Any] = {"IsTruncated": index + 1 < len(self.pages)}
        if page["IsTruncated"]:
            page["NextContinuationToken"] = str(index + 1)
        if self.pages[index]:
            page["Contents"] = [
                {"Key": key, "LastModified": datetime.fromtimestamp(at, UTC)} for key, at in self.pages[index].items()
            ]
        return page

    def get_object(self, Bucket: str, Key: str) -> dict[str, io.BytesIO]:
        assert Bucket == "bucket"
        return {"Body": io.BytesIO(self.bodies[Key])}


class Endless(Bucket):
    def list_objects_v2(self, **request: str) -> dict[str, Any]:
        self.requests.append(request)
        return {"IsTruncated": True, "NextContinuationToken": "next"}


class Refusing(Bucket):
    def list_objects_v2(self, **request: str) -> dict[str, Any]:
        self.requests.append(request)
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "Access Denied"}}, "ListObjectsV2")


class Unreachable(Bucket):
    def list_objects_v2(self, **request: str) -> dict[str, Any]:
        self.requests.append(request)
        raise EndpointConnectionError(endpoint_url="http://localhost:9000")


def rows(unit: str, *tests: str) -> bytes:
    return "".join(
        json.dumps(
            {
                "test": test,
                "run": unit,
                "run_attempt": 2,
                "attempts": 2,
                "outcome": "flaky",
                "errors": ["AssertionError"],
                "tracebacks": [],
            }
        )
        + "\n"
        for test in tests
    ).encode()


def stored(root: Path, unit: str, data: bytes, written_s: float) -> None:
    path = objects.flakes_path("test", unit)
    objects.LocalObjects(root).write_once(path, data)
    os.utime(root / path, (0, written_s))


def run(*argv: str, environ: Mapping[str, str] | None = None, now_s: float = NOW_S) -> int:
    parser = argparse.ArgumentParser()
    flakes.declare(parser)
    return flakes.command(parser.parse_args(argv), environ or {}, now_s)


def budget(tmp_path: Path, reports: Path, now_s: float) -> dict[str, Any]:
    out = tmp_path / "budget.json"
    history = tmp_path / "history.jsonl"
    argv = ["--history", str(history), "--reports", str(reports), "--out", str(out), "--now", str(now_s)]
    assert flake_budget.main(argv) == 0
    return json.loads(out.read_text())


def bucket_store(monkeypatch: pytest.MonkeyPatch, bucket: Bucket, prefix: str) -> list[Mapping[str, str]]:
    built: list[Mapping[str, str]] = []

    def storage(environ: Mapping[str, str]) -> objects.S3Objects:
        built.append(environ)
        return objects.S3Objects(bucket, "bucket", prefix)

    monkeypatch.setattr(scheduler, "storage", storage)
    return built


@pytest.mark.feature("lab-flakes-export")
def test_two_exports_of_one_flaky_unit_count_its_test_once_in_the_flake_budget(tmp_path: Path) -> None:
    store = tmp_path / "objects"
    stored(store, FIRST, rows(FIRST, "tests/test_x.py::test_flaky[1]"), NOW_S - 60)

    assert run("--objects", str(store), "--out", str(tmp_path / "first")) == 0
    first = budget(tmp_path, tmp_path / "first", NOW_S)
    assert run("--objects", str(store), "--out", str(tmp_path / "second"), now_s=NOW_S + 3600) == 0
    second = budget(tmp_path, tmp_path / "second", NOW_S + 3600)

    once = {"test": "tests/test_x.py::test_flaky", "count": 1, "runs": [FIRST]}
    assert first == {"flaky_tests": 1, "over_budget": [], "retried": [once]}
    assert second == first
    history = [json.loads(line) for line in (tmp_path / "history.jsonl").read_text().splitlines()]
    assert history == [{"test": "tests/test_x.py::test_flaky", "run": FIRST, "at": NOW_S}]


@pytest.mark.feature("lab-flakes-export")
@pytest.mark.parametrize("backend", ["local", "s3"])
def test_an_empty_store_writes_an_empty_file_and_the_budget_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    store = tmp_path / "objects"
    store.mkdir()
    out = tmp_path / "reports"
    out.mkdir()
    bucket = Bucket([{}], {})
    bucket_store(monkeypatch, bucket, "")
    argv = ("--objects", str(store)) if backend == "local" else ()

    assert run(*argv, "--out", str(out), environ=SETTINGS) == 0

    assert capsys.readouterr().out == f"{out / 'lab-flakes.jsonl'}\n"
    assert (out / "lab-flakes.jsonl").read_bytes() == b""
    assert len(bucket.requests) == (backend == "s3")
    assert budget(tmp_path, out, NOW_S) == {"flaky_tests": 0, "over_budget": [], "retried": []}


@pytest.mark.feature("lab-flakes-export")
@pytest.mark.parametrize(("argv", "window_s"), [((), WEEK_S), (("--hours", "168"), WEEK_S), (("--hours", "1"), 3600)])
def test_the_window_keeps_an_object_written_at_its_start_and_drops_one_a_second_older(
    tmp_path: Path, argv: tuple[str, ...], window_s: int
) -> None:
    store = tmp_path / "objects"
    stored(store, FIRST, rows(FIRST, "tests/test_x.py::test_start"), NOW_S - window_s)
    stored(store, SECOND, rows(SECOND, "tests/test_x.py::test_older"), NOW_S - window_s - 1)
    report = objects.unit_path("test", FIRST, "report")
    objects.LocalObjects(store).write_once(report, b'{"flaky": true}\n')
    os.utime(store / report, (0, NOW_S))
    out = tmp_path / "nested" / "reports"

    assert run("--objects", str(store), "--out", str(out), *argv) == 0

    assert (out / "lab-flakes.jsonl").read_bytes() == rows(FIRST, "tests/test_x.py::test_start")


@pytest.mark.feature("lab-flakes-export")
def test_the_s3_listing_follows_continuation_pages_honours_the_store_prefix_and_keeps_window_objects_in_path_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second, old = (f"store/{objects.flakes_path('test', unit)}" for unit in (FIRST, SECOND, OLD))
    bodies = {first: rows(FIRST, "tests/test_x.py::test_a"), second: rows(SECOND, "tests/test_x.py::test_b")}
    bucket = Bucket([{second: NOW_S - 10, old: NOW_S - WEEK_S - 1}, {first: NOW_S - WEEK_S}], bodies)
    built = bucket_store(monkeypatch, bucket, "store/")
    out = tmp_path / "reports"

    assert run("--out", str(out), environ=SETTINGS) == 0

    assert built == [SETTINGS]
    assert bucket.requests == [
        {"Bucket": "bucket", "Prefix": "store/lab/flakes/"},
        {"Bucket": "bucket", "Prefix": "store/lab/flakes/", "ContinuationToken": "1"},
    ]
    assert (out / "lab-flakes.jsonl").read_bytes() == bodies[first] + bodies[second]


@pytest.mark.feature("lab-flakes-export")
@pytest.mark.parametrize("name", sorted(SETTINGS))
def test_a_missing_store_setting_exits_one_naming_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    out = tmp_path / "reports"

    assert run("--out", str(out), environ={key: value for key, value in SETTINGS.items() if key != name}) == 1

    assert capsys.readouterr() == ("", f"ci lab flakes: missing setting {name}\n")
    assert not out.exists()


@pytest.mark.feature("lab-flakes-export")
@pytest.mark.parametrize(
    ("client", "reason", "requests"),
    [
        (Endless, f"listing lab/flakes/ did not finish within {PAGES} pages", PAGES),
        (Refusing, DENIED, 1),
        (Unreachable, UNREACHABLE, 1),
    ],
)
def test_an_object_store_error_exits_one_with_one_plain_line(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    client: type[Bucket],
    reason: str,
    requests: int,
) -> None:
    bucket = client([], {})
    bucket_store(monkeypatch, bucket, "")
    out = tmp_path / "reports"

    assert run("--out", str(out), environ=SETTINGS) == 1

    assert capsys.readouterr() == ("", f"ci lab flakes: {reason}\n")
    assert len(bucket.requests) == requests
    assert not out.exists()


@pytest.mark.feature("lab-flakes-export")
def test_an_objects_path_that_is_not_a_folder_exits_one_naming_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "objects.txt"
    store.write_text("")
    out = tmp_path / "reports"

    assert run("--objects", str(store), "--out", str(out)) == 1

    assert capsys.readouterr() == ("", f"ci lab flakes: not a folder: {store}\n")
    assert not out.exists()


@pytest.mark.feature("lab-flakes-export")
@pytest.mark.parametrize(
    ("argv", "error"),
    [
        ((), "the following arguments are required: --out"),
        (("--out", "reports", "--hours", "0"), "argument --hours: 0 is not 1 to 168 hours"),
        (("--out", "reports", "--hours", "169"), "argument --hours: 169 is not 1 to 168 hours"),
    ],
)
def test_ci_lab_flakes_refuses_a_missing_output_folder_and_a_window_outside_the_budget_history(
    capsys: pytest.CaptureFixture[str], argv: tuple[str, ...], error: str
) -> None:
    with pytest.raises(SystemExit) as refused:
        evidence.ci(["lab", "flakes", *argv])

    assert refused.value.code == 2
    assert capsys.readouterr().err.splitlines()[-1] == f"agentihooks ci lab flakes: error: {error}"


@pytest.mark.feature("lab-flakes-export")
def test_ci_lab_flakes_resolves_through_the_ci_delegate_and_prints_the_written_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "objects"
    data = rows(FIRST, "tests/test_x.py::test_flaky")
    objects.LocalObjects(store).write_once(objects.flakes_path("test", FIRST), data)
    stored(store, OLD, rows(OLD, "tests/test_x.py::test_old"), time.time() - WEEK_S - 3600)
    out = tmp_path / "reports"
    argv = ["ci", "lab", "flakes", "--objects", str(store), "--out", str(out)]

    assert delegated_cli(argv) is evidence.ci
    assert evidence.ci(argv[1:]) == 0

    assert capsys.readouterr().out == f"{out / 'lab-flakes.jsonl'}\n"
    assert (out / "lab-flakes.jsonl").read_bytes() == data


@pytest.mark.feature("lab-flakes-export")
def test_ci_lab_flakes_reads_the_object_store_named_by_the_environment_and_exits_one_on_its_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in SETTINGS.items():
        monkeypatch.setenv(name, value)
    bucket = Refusing([], {})
    built = bucket_store(monkeypatch, bucket, "")
    out = tmp_path / "reports"

    assert evidence.ci(["lab", "flakes", "--out", str(out)]) == 1

    assert [{name: environ[name] for name in SETTINGS} for environ in built] == [SETTINGS]
    assert capsys.readouterr() == ("", f"ci lab flakes: {DENIED}\n")
    assert not out.exists()
