import io
import json
import subprocess
import threading
import zipfile
from pathlib import Path

import pytest

from scripts import ci_test_plan

REPOSITORY = "the-cloud-clockwork/agentihooks"
HEAD = "a" * 40
ACTIONS = f"repos/{REPOSITORY}/actions"
LATER = "2026-10-17T00:00:00Z"

pytestmark = pytest.mark.unit


def _run(run_id, **changes):
    return {
        "id": run_id,
        "run_attempt": 1,
        "event": "push",
        "head_branch": "dev",
        "head_sha": f"{run_id:040d}",
        "status": "completed",
        "updated_at": f"2026-10-16T22:{run_id:02d}:00Z",
        "path": ".github/workflows/test.yml",
        "repository": {"full_name": REPOSITORY},
        **changes,
    }


def _zip(manifest):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
    return buffer.getvalue()


def _github(monkeypatch, runs, *, manifests, failed=(), uncovered=()):
    listing = f"{ACTIONS}/workflows/test.yml/runs?branch=dev&event=push&status=completed&per_page=10"

    def api(endpoint):
        if endpoint == listing:
            return json.dumps({"workflow_runs": runs}).encode()
        assert endpoint.startswith(f"{ACTIONS}/")
        if endpoint.endswith("/artifacts?per_page=100"):
            run_id = int(endpoint.split("/runs/")[1].split("/")[0])
            kept = [{"id": run_id, "name": "dep-manifest", "expired": False}] if run_id in manifests else []
            kept += [
                {"id": 0, "name": name, "expired": run_id in uncovered and name == "coverage-3.12-12"}
                for name in sorted(ci_test_plan.COVERAGE)
            ]
            return json.dumps({"artifacts": [{"id": 0, "name": "durations-merged", "expired": False}, *kept]}).encode()
        assert endpoint.endswith("/zip")
        return _zip(manifests[int(endpoint.split("/artifacts/")[1].split("/")[0])])

    def jobs(endpoint, size=None):
        assert endpoint.startswith(f"{ACTIONS}/runs/") and endpoint.endswith("/attempts/1/jobs")
        run_id = int(endpoint.split("/runs/")[1].split("/")[0])
        return [
            {"name": name, "conclusion": "failure" if (run_id, name) in failed else "success"}
            for name in sorted(ci_test_plan.UNIT_JOBS)
        ]

    monkeypatch.setattr(ci_test_plan, "_api", api)
    monkeypatch.setattr(ci_test_plan, "read_jobs", jobs)


def _manifest(run_id):
    return {
        "commit": f"{run_id:040d}",
        "units": ["hooks/a.py::a"],
        "tests": {"tests/test_a.py::test_a": {"units": [0], "files": [], "escapes": False}},
    }


def test_the_api_reads_through_gh_and_fails_on_an_error(monkeypatch):
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, stdout=b"{}")

    monkeypatch.setattr(ci_test_plan.subprocess, "run", run)
    assert ci_test_plan._api("repos/x") == b"{}"
    assert calls == [((["gh", "api", "repos/x"],), {"capture_output": True, "check": True, "timeout": 60})]


@pytest.mark.parametrize(
    "message", ["HTTP 403: Resource not accessible by integration", "HTTP 404: Not Found", "HTTP 502: Bad Gateway"]
)
def test_a_failed_record_download_names_the_github_error_and_stays_red(monkeypatch, tmp_path, capsys, message):
    download = ci_test_plan._api
    _github(monkeypatch, [_run(1)], manifests={1: _manifest(1)})
    api = ci_test_plan._api

    def fetch(endpoint):
        return download(endpoint) if endpoint.endswith("/zip") else api(endpoint)

    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, stderr=message.encode())

    monkeypatch.setattr(ci_test_plan, "_api", fetch)
    monkeypatch.setattr(ci_test_plan.subprocess, "run", fail)
    out = tmp_path / "reused-tests.json"
    with pytest.raises(subprocess.CalledProcessError):
        ci_test_plan.main(["--repository", REPOSITORY, "--head", HEAD, "--before", LATER, "--out", str(out)])
    assert message in capsys.readouterr().err
    assert not out.exists()


def test_the_newest_dev_run_whose_unit_shards_passed_and_kept_a_manifest_is_the_record(monkeypatch):
    runs = [_run(4, event="merge_group"), _run(3), _run(2), _run(1)]
    _github(monkeypatch, runs, manifests={2: _manifest(2), 1: _manifest(1)}, failed={(2, "unit (3.12, 4)")})
    run, manifest = ci_test_plan.find_record(REPOSITORY, LATER)
    assert run["id"] == 1
    assert manifest["commit"] == f"{1:040d}"


def test_a_record_reads_all_jobs_of_its_run_in_one_request(monkeypatch):
    from scripts.ci_budget import jobs as listing

    _github(monkeypatch, [_run(1)], manifests={1: _manifest(1)})
    monkeypatch.setattr(ci_test_plan, "read_jobs", listing.read_jobs)
    rows = [{"name": name, "conclusion": "success"} for name in sorted(ci_test_plan.UNIT_JOBS)]
    rows += [{"name": f"gate {index}", "conclusion": "success"} for index in range(60)]
    requests = []

    def run(command, **kwargs):
        query = dict(item.split("=") for item in command[-1].partition("?")[2].split("&"))
        size, page = int(query["per_page"]), int(query["page"])
        requests.append(command[-1])
        return subprocess.CompletedProcess(command, 0, json.dumps({"jobs": rows[(page - 1) * size : page * size]}), "")

    monkeypatch.setattr(listing.subprocess, "run", run)
    assert ci_test_plan.find_record(REPOSITORY, LATER)[0]["id"] == 1
    assert len(requests) == 1


def test_the_candidate_runs_list_their_artifacts_at_the_same_time(monkeypatch):
    runs = [_run(4), _run(3), _run(2), _run(1)]
    _github(monkeypatch, runs, manifests={1: _manifest(1)})
    api = ci_test_plan._api
    listings = threading.Barrier(len(runs), timeout=10)

    def concurrent(endpoint):
        if endpoint.endswith("/artifacts?per_page=100"):
            listings.wait()
        return api(endpoint)

    monkeypatch.setattr(ci_test_plan, "_api", concurrent)
    assert ci_test_plan.find_record(REPOSITORY, LATER)[0]["id"] == 1


def test_a_record_downloads_while_its_jobs_are_read(monkeypatch):
    _github(monkeypatch, [_run(1)], manifests={1: _manifest(1)})
    api, jobs = ci_test_plan._api, ci_test_plan.read_jobs
    both = threading.Barrier(2, timeout=10)

    def download(endpoint):
        if endpoint.endswith("/zip"):
            both.wait()
        return api(endpoint)

    def listing(endpoint, size=None):
        both.wait()
        return jobs(endpoint, size)

    monkeypatch.setattr(ci_test_plan, "_api", download)
    monkeypatch.setattr(ci_test_plan, "read_jobs", listing)
    assert ci_test_plan.find_record(REPOSITORY, LATER)[0]["id"] == 1


def test_a_run_whose_unit_shards_failed_never_reads_its_download(monkeypatch):
    _github(
        monkeypatch, [_run(2), _run(1)], manifests={2: _manifest(2), 1: _manifest(1)}, failed={(2, "unit (3.12, 4)")}
    )
    api = ci_test_plan._api
    monkeypatch.setattr(
        ci_test_plan, "_api", lambda endpoint: b"broken" if endpoint.endswith("/2/zip") else api(endpoint)
    )
    assert ci_test_plan.find_record(REPOSITORY, LATER)[0]["id"] == 1


def test_every_interpreter_leg_ignores_dev_runs_that_finished_after_its_run_started(monkeypatch):
    _github(monkeypatch, [_run(3), _run(1)], manifests={3: _manifest(3), 1: _manifest(1)})
    assert ci_test_plan.find_record(REPOSITORY, "2026-10-16T22:02:00Z")[0]["id"] == 1
    assert ci_test_plan.find_record(REPOSITORY, "2026-10-16T22:03:00Z")[0]["id"] == 3


@pytest.mark.parametrize(
    "field,value",
    [("event", "merge_group"), ("head_branch", "feature"), ("path", ".github/workflows/proofs.yml")],
)
def test_runs_that_are_not_dev_pushes_of_the_tests_workflow_are_no_record(monkeypatch, field, value):
    _github(monkeypatch, [_run(1, **{field: value})], manifests={1: _manifest(1)})
    assert ci_test_plan.find_record(REPOSITORY, LATER) is None


def test_a_manifest_for_another_commit_is_skipped_for_an_older_record(monkeypatch, capsys):
    _github(monkeypatch, [_run(2), _run(1)], manifests={2: _manifest(9), 1: _manifest(1)})
    assert ci_test_plan.find_record(REPOSITORY, LATER)[0]["id"] == 1
    assert capsys.readouterr().out == f"Dev run 2 recorded a manifest for {9:040d}, not {2:040d}\n"


def test_a_record_whose_shard_coverage_expired_is_skipped(monkeypatch):
    _github(monkeypatch, [_run(2), _run(1)], manifests={2: _manifest(2), 1: _manifest(1)}, uncovered={2})
    assert ci_test_plan.find_record(REPOSITORY, LATER)[0]["id"] == 1


def test_a_missing_record_plans_a_full_run(monkeypatch, tmp_path, capsys):
    _github(monkeypatch, [_run(1)], manifests={})
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    out = tmp_path / "reused-tests.json"
    assert ci_test_plan.main(["--repository", REPOSITORY, "--head", HEAD, "--before", LATER, "--out", str(out)]) == 0
    assert not out.exists()
    assert output.read_text() == "reused=0\nrun=\ncommit=\n"
    assert "every test runs" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("content", "zipped"),
    [
        (b"", False),
        (b"not a zip", False),
        (b"{}", "corrupt-deflate"),
        (b"", True),
        (b"{", True),
        (bytes([255]), True),
        (b"[]", True),
        (b"{}", True),
        (json.dumps({**_manifest(1), "tests": {}}).encode(), True),
        (json.dumps({**_manifest(1), "tests": []}).encode(), True),
        (json.dumps({**_manifest(1), "units": {}}).encode(), True),
        (json.dumps({**_manifest(1), "tests": {"test": {}}}).encode(), True),
        (json.dumps({**_manifest(1), "tests": {"test": None}}).encode(), True),
        (
            json.dumps({**_manifest(1), "tests": {"test": {"units": [0], "files": None, "escapes": False}}}).encode(),
            True,
        ),
        (json.dumps({**_manifest(1), "tests": {"test": {"units": [0], "files": [], "escapes": None}}}).encode(), True),
        (
            json.dumps({**_manifest(1), "tests": {"test": {"units": [-1], "files": [], "escapes": False}}}).encode(),
            True,
        ),
        (json.dumps({**_manifest(1), "tests": {"test": {"units": [1], "files": [], "escapes": False}}}).encode(), True),
        (
            json.dumps({**_manifest(1), "tests": {"test": {"units": [True], "files": [], "escapes": False}}}).encode(),
            True,
        ),
        (
            json.dumps({**_manifest(1), "tests": {"test": {"units": [0.0], "files": [], "escapes": False}}}).encode(),
            True,
        ),
    ],
    ids=[
        "empty-download",
        "corrupt-zip",
        "corrupt-deflate",
        "empty-json",
        "corrupt-json",
        "invalid-encoding",
        "non-object",
        "missing-fields",
        "empty-tests",
        "wrong-tests",
        "wrong-units",
        "wrong-test",
        "non-object-test",
        "wrong-files",
        "wrong-escapes",
        "negative-unit",
        "unit-out-of-range",
        "boolean-unit",
        "float-unit",
    ],
)
def test_an_unusable_download_runs_every_test_and_names_its_own_fallback(
    monkeypatch, tmp_path, capsys, content, zipped
):
    _github(monkeypatch, [_run(1)], manifests={1: _manifest(1)})
    api = ci_test_plan._api
    buffer = io.BytesIO()
    if zipped:
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", content)
    payload = buffer.getvalue() if zipped else content
    if zipped == "corrupt-deflate":
        offset = 30 + len("manifest.json")
        payload = payload[:offset] + bytes([7]) + payload[offset + 1 :]
    monkeypatch.setattr(ci_test_plan, "_api", lambda endpoint: payload if endpoint.endswith("/zip") else api(endpoint))
    monkeypatch.setattr(
        ci_test_plan.dep_manifest, "changes", lambda *args: pytest.fail("Unusable records cannot plan reuse")
    )
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    out = tmp_path / "reused-tests.json"
    assert ci_test_plan.main(["--repository", REPOSITORY, "--head", HEAD, "--before", LATER, "--out", str(out)]) == 0
    assert not out.exists()
    assert output.read_text() == "reused=0\nrun=\ncommit=\n"
    report = capsys.readouterr().out
    assert "Unusable test record" in report
    assert "Dev run 1" in report
    assert "every test runs" in report
    assert "No test record" not in report
    if not zipped:
        reason = "File is not a zip file"
    elif zipped == "corrupt-deflate":
        reason = "Error -3 while decompressing data: invalid block type"
    elif content == b"":
        reason = "Expecting value: line 1 column 1 (char 0)"
    elif content == b"{":
        reason = "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"
    elif content == bytes([255]):
        reason = "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"
    else:
        reason = "dependency manifest schema is invalid or empty"
    assert report == f"::warning title=Unusable test record::Dev run 1: {reason}; every test runs.\n"


@pytest.mark.parametrize("cause", [None, ".github/workflows/test.yml"])
def test_a_record_plans_the_unchanged_tests(monkeypatch, tmp_path, capsys, cause):
    _github(monkeypatch, [_run(1)], manifests={1: _manifest(1)})
    seen = []
    changed = [(cause, None, None)] if cause else []
    monkeypatch.setattr(
        ci_test_plan.dep_manifest, "changes", lambda repo, base, head: seen.append((repo, base, head)) or changed
    )
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    out = tmp_path / "reused-tests.json"
    assert ci_test_plan.main(["--repository", REPOSITORY, "--head", HEAD, "--before", LATER, "--out", str(out)]) == 0
    assert seen == [(Path.cwd(), f"{1:040d}", HEAD)]
    assert isinstance(seen[0][0], Path)
    plan = json.loads(out.read_text())
    assert plan["run"] == "1"
    if cause:
        assert plan["tests"] == []
        assert output.read_text() == f"reused=0\nrun=1\ncommit={1:040d}\n"
        assert capsys.readouterr().out == f"Dev run 1 on {1:040d}: 1 of 1 tests invalidated by {cause}, 0 reused\n"
    else:
        assert plan["tests"] == ["tests/test_a.py::test_a"]
        assert output.read_text() == f"reused=1\nrun=1\ncommit={1:040d}\n"
        assert capsys.readouterr().out == f"Dev run 1 on {1:040d}: 0 of 1 tests invalidated, 1 reused\n"


def test_the_repo_flag_names_the_diffed_checkout(monkeypatch, tmp_path):
    _github(monkeypatch, [_run(1)], manifests={1: _manifest(1)})
    seen = []
    monkeypatch.setattr(ci_test_plan.dep_manifest, "changes", lambda repo, base, head: seen.append(repo) or [])
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    args = ["--repository", REPOSITORY, "--head", HEAD, "--before", LATER, "--repo", str(tmp_path)]
    args += ["--out", str(tmp_path / "p.json")]
    assert ci_test_plan.main(args) == 0
    assert seen == [tmp_path]


@pytest.mark.parametrize("missing", ["--repository", "--head", "--before", "--out"])
def test_every_required_flag_is_required(tmp_path, missing):
    flags = {"--repository": REPOSITORY, "--head": HEAD, "--before": LATER, "--out": str(tmp_path / "p.json")}
    args = [part for flag, value in flags.items() if flag != missing for part in (flag, value)]
    with pytest.raises(SystemExit):
        ci_test_plan.main(args)


def test_the_before_flag_reaches_the_record_lookup(monkeypatch, tmp_path):
    _github(monkeypatch, [_run(3), _run(1)], manifests={3: _manifest(3), 1: _manifest(1)})
    monkeypatch.setattr(ci_test_plan.dep_manifest, "changes", lambda repo, base, head: [])
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    out = tmp_path / "p.json"
    args = ["--repository", REPOSITORY, "--head", HEAD, "--before", "2026-10-16T22:02:00Z", "--out", str(out)]
    assert ci_test_plan.main(args) == 0
    assert json.loads(out.read_text())["run"] == "1"
