import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from scripts.ci_budget import jobs as reader

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
READER = "Read this attempt's jobs"
FAKE = r"""
import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

endpoint = next(arg for arg in sys.argv if arg.startswith("repos/"))
if not urlsplit(endpoint).path.endswith("/jobs"):
    print(json.dumps({"run_started_at": "2026-10-13T00:00:00Z"}))
    sys.exit(0)
query = parse_qs(urlsplit(endpoint).query)
size = int(query.get("per_page", [30])[0])
page = int(query.get("page", [1])[0])
start = (page - 1) * size
with Path(os.environ["REQUESTS"]).open("a") as handle:
    handle.write(json.dumps([size, page]) + "\n")
fault = os.environ["FAULT"]
if fault == "client":
    print("gh: Not Found (HTTP 404)", file=sys.stderr)
    sys.exit(1)
if fault == "server" or fault == "timeout":
    message = "gh: Bad Gateway (HTTP 502)" if fault == "server" else "gh: i/o timeout"
    print(message, file=sys.stderr)
    sys.exit(1)
if fault == "shrink" and (size > 5 or start >= 15 and size > 2):
    print("gh: Bad Gateway (HTTP 502)", file=sys.stderr)
    sys.exit(1)
print(json.dumps({"total_count": 23, "jobs": [{"id": i} for i in range(start, min(start + size, 23))]}))
"""


def _read(tmp_path, fault):
    gh = tmp_path / "gh"
    gh.write_text(f"#!{sys.executable}\n{FAKE}")
    gh.chmod(0o755)
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    step = next(step for step in workflow["jobs"]["stage-budget"]["steps"] if step.get("name") == READER)
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", step["run"]],
        cwd=ROOT,
        env={
            "PATH": f"{tmp_path}:{Path(sys.executable).parent}:{os.defpath}",
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_REPOSITORY": "owner/repo",
            "GITHUB_RUN_ID": "7",
            "GITHUB_RUN_ATTEMPT": "2",
            "REQUESTS": str(tmp_path / "requests.jsonl"),
            "FAULT": fault,
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    requests = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()]
    jobs = [json.loads(line) for line in (tmp_path / "jobs.jsonl").read_text().splitlines()]
    return result, requests, jobs


@pytest.mark.parametrize("fault", ["healthy", "shrink"])
def test_workflow_reader_lists_every_job_with_small_pages_after_server_errors(tmp_path, fault):
    result, requests, jobs = _read(tmp_path, fault)
    assert result.returncode == 0, result.stderr
    assert jobs == [{"id": i} for i in range(23)]
    assert max(size for size, _ in requests) <= 10
    if fault == "shrink":
        assert requests[:2] == [[10, 1], [5, 1]]
        assert [5, 4] in requests and [2, 8] in requests


def test_workflow_reader_fails_client_errors_without_retry(tmp_path):
    result, requests, jobs = _read(tmp_path, "client")
    assert result.returncode != 0
    assert "HTTP 404" in result.stderr
    assert len(requests) == 1
    assert jobs == []


@pytest.mark.parametrize("fault", ["server", "timeout"])
def test_workflow_reader_stops_after_bounded_retries(tmp_path, fault):
    result, requests, jobs = _read(tmp_path, fault)
    assert result.returncode != 0
    assert requests == [[10, 1], [5, 1], [2, 1], [1, 1]]
    assert jobs == []


@pytest.mark.parametrize(
    "error",
    [
        subprocess.TimeoutExpired(["gh"], 10),
        subprocess.CalledProcessError(1, ["gh"], stderr="gh: Server Error (HTTP 500)"),
        subprocess.CalledProcessError(1, ["gh"], stderr="gh: Bad Gateway (HTTP 502)"),
        subprocess.CalledProcessError(1, ["gh"], stderr="gh: I/O TIMEOUT"),
        subprocess.CalledProcessError(1, ["gh"], stderr="gh: request timed out"),
    ],
)
def test_reader_retries_timeout_or_server_error_with_short_backoff(monkeypatch, error):
    calls, sleeps = [], []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if len(calls) == 1:
            raise error
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"jobs": [{"id": 15}]}))

    monkeypatch.setattr(reader.subprocess, "run", run)
    monkeypatch.setattr(reader.time, "sleep", sleeps.append)
    assert reader.read_page("repos/owner/repo/jobs", 15, 5) == ([{"id": 15}], 2)
    assert all(command[:2] == ["gh", "api"] for command, _ in calls)
    assert [command[-1] for command, _ in calls] == [
        "repos/owner/repo/jobs?per_page=5&page=4",
        "repos/owner/repo/jobs?per_page=2&page=8",
    ]
    assert all(kwargs == {"capture_output": True, "text": True, "check": True, "timeout": 10} for _, kwargs in calls)
    assert sleeps == [1]


@pytest.mark.parametrize("status", [400, 404, 429, 499, 600])
def test_reader_does_not_retry_a_non_server_http_error(monkeypatch, status):
    calls = []
    error = subprocess.CalledProcessError(1, ["gh"], stderr=f"gh: timeout (HTTP {status})")

    def run(command, **kwargs):
        calls.append(command)
        raise error

    monkeypatch.setattr(reader.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        reader.read_page("repos/owner/repo/jobs", 0, 10)
    assert caught.value is error
    assert len(calls) == 1


@pytest.mark.parametrize("count", [0, 20, 23])
@pytest.mark.parametrize("shrink", [False, True])
def test_reader_preserves_every_job_when_page_size_changes(monkeypatch, count, shrink):
    calls, sleeps = [], []

    def run(command, **kwargs):
        assert len(calls) < count + 5
        assert command[:2] == ["gh", "api"]
        assert urlsplit(command[-1]).path == "repos/owner/repo/jobs"
        query = parse_qs(urlsplit(command[-1]).query)
        size, page = int(query["per_page"][0]), int(query["page"][0])
        calls.append((size, page))
        start = (page - 1) * size
        if shrink and (size > 5 or start >= 15 and size > 2):
            raise subprocess.CalledProcessError(1, command, stderr="gh: Server Error (HTTP 503)")
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps({"jobs": [{"id": i} for i in range(start, min(start + size, count))]})
        )

    monkeypatch.setattr(reader.subprocess, "run", run)
    monkeypatch.setattr(reader.time, "sleep", sleeps.append)
    assert reader.read_jobs("repos/owner/repo/jobs") == [{"id": i} for i in range(count)]
    assert max(size for size, _ in calls) == 10
    if shrink:
        assert calls[:2] == [(10, 1), (5, 1)]
        assert sleeps == ([1] if count == 0 else [1, 1])


@pytest.mark.parametrize(
    "error",
    [
        subprocess.TimeoutExpired(["gh"], 10),
        subprocess.CalledProcessError(1, ["gh"], stderr="gh: Server Error (HTTP 599)"),
    ],
)
def test_reader_exhausts_retries_without_partial_results(monkeypatch, error):
    calls, sleeps = [], []

    def run(command, **kwargs):
        calls.append(command[-1])
        raise error

    monkeypatch.setattr(reader.subprocess, "run", run)
    monkeypatch.setattr(reader.time, "sleep", sleeps.append)
    with pytest.raises(type(error)) as caught:
        reader.read_jobs("repos/owner/repo/jobs")
    assert caught.value is error
    assert calls == [f"repos/owner/repo/jobs?per_page={size}&page=1" for size in [10, 5, 2, 1]]
    assert sleeps == [1, 2, 3]


def test_reader_fails_an_unknown_error_without_retry(monkeypatch):
    calls = []
    error = subprocess.CalledProcessError(1, ["gh"], stderr="gh: connection refused")

    def run(command, **kwargs):
        calls.append(command)
        raise error

    monkeypatch.setattr(reader.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError) as caught:
        reader.read_jobs("repos/owner/repo/jobs")
    assert caught.value is error
    assert len(calls) == 1


def test_reader_accepts_a_five_job_initial_page_size(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command[-1])
        page = int(parse_qs(urlsplit(command[-1]).query)["page"][0])
        rows = {1: [{"id": n} for n in range(5)], 2: [{"id": n} for n in range(5, 10)], 3: [{"id": 10}]}[page]
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"jobs": rows}))

    monkeypatch.setattr(reader.subprocess, "run", run)
    assert reader.read_jobs("repos/owner/repo/jobs", 5) == [{"id": n} for n in range(11)]
    assert calls == [
        "repos/owner/repo/jobs?per_page=5&page=1",
        "repos/owner/repo/jobs?per_page=5&page=2",
        "repos/owner/repo/jobs?per_page=5&page=3",
    ]


@pytest.mark.parametrize("size", [0, -1, 101])
def test_reader_cli_refuses_a_page_size_outside_the_github_range(size):
    result = subprocess.run(
        [sys.executable, "-m", "scripts.ci_budget.jobs", "repos/owner/repo/jobs", "--page-size", str(size)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "invalid choice" in result.stderr
