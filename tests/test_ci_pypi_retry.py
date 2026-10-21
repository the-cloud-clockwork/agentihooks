import os
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
RETRY = ROOT / ".github/scripts/pypi-retry.sh"
LABEL = "::error title=Python package index unavailable::"
BAD_GATEWAY = "  Caused by: HTTP status server error (502 Bad Gateway) for url (https://pypi.org/simple/setuptools/)\n"


def _bin(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "sleep").write_text(f'#!/usr/bin/env bash\necho "$1" >> "{tmp_path}/sleeps"\n')
    (bin_dir / "sleep").chmod(0o755)
    return bin_dir


def _fake(tmp_path, answers):
    bin_dir = _bin(tmp_path)
    queue = tmp_path / "answers"
    queue.mkdir()
    for index, (err, status) in enumerate(answers):
        (queue / f"{index:03}.err").write_text(err)
        (queue / f"{index:03}.status").write_text(str(status))
    command = bin_dir / "install"
    command.write_text(
        "#!/usr/bin/env bash\n"
        f'n=$(ls "{queue}"/*.status | head -n1); base="${{n%.status}}"\n'
        'cat "$base.err" >&2; status=$(cat "$n")\n'
        f'echo "$*" >> "{tmp_path}/calls"\n'
        'rm "$base.err" "$n"\n'
        'exit "$status"\n'
    )
    command.chmod(0o755)
    return bin_dir


def _run(tmp_path, bin_dir, command, **env):
    summary = tmp_path / "summary"
    result = subprocess.run(
        ["bash", str(RETRY), "Install dependencies", "--", *command],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "GITHUB_STEP_SUMMARY": str(summary), **env},
        capture_output=True,
        text=True,
        timeout=60,
    )
    sleeps = [int(s) for s in (tmp_path / "sleeps").read_text().split()] if (tmp_path / "sleeps").exists() else []
    return result, sleeps, summary.read_text() if summary.exists() else ""


def _retry(tmp_path, answers, **env):
    result, sleeps, summary = _run(tmp_path, _fake(tmp_path, answers), ["install", "-e", ".[dev,all]"], **env)
    calls = (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []
    return result, calls, sleeps, summary


@pytest.mark.parametrize(
    "error",
    [
        BAD_GATEWAY,
        "  Caused by: HTTP status server error (503 Service Unavailable) for url (https://pypi.org/simple/pytest/)\n",
        "  Caused by: HTTP status client error (429 Too Many Requests) for url (https://pypi.org/simple/pytest/)\n",
        "  Caused by: error sending request for url (https://pypi.org/simple/pytest/)\n",
        "  Caused by: tcp connect error: Connection refused (os error 111)\n",
        "  Caused by: Connection reset by peer (os error 104)\n",
        "  Caused by: dns error: failed to lookup address information\n",
        "  Caused by: operation timed out\n",
        "  Caused by: deadline has elapsed\n",
    ],
    ids=["bad-gateway", "unavailable", "too-many", "send", "refused", "reset", "dns", "timeout", "deadline"],
)
def test_a_short_index_outage_is_retried_with_backoff_until_the_install_succeeds(tmp_path, error):
    result, calls, sleeps, summary = _retry(tmp_path, [(error, 2), (error, 2), ("Installed 90 packages\n", 0)])
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 3
    assert calls[0] == calls[2] == "-e .[dev,all]"
    assert sleeps == [5, 10]
    assert result.stderr.count("::warning title=Python package index retry::") == 2
    assert "Installed 90 packages" in result.stdout + result.stderr
    assert LABEL not in result.stdout + result.stderr
    assert summary == ""


def test_an_index_that_stays_down_retries_for_two_minutes_then_names_the_outage(tmp_path):
    result, calls, sleeps, summary = _retry(tmp_path, [(BAD_GATEWAY, 2)] * 20)
    assert result.returncode == 2
    assert sleeps == [5, 10, 20, 30, 30, 30, 30]
    assert sum(sleeps) >= 120
    assert len(calls) == 8
    assert LABEL in result.stderr
    message = result.stderr.split(LABEL)[1]
    assert "Install dependencies failed 8 times over 155s" in message
    assert "502 Bad Gateway" in message
    assert "not a code verdict" in message
    assert "Python package index unavailable" in summary


def test_the_retry_budget_is_configurable(tmp_path):
    result, calls, sleeps, _ = _retry(tmp_path, [(BAD_GATEWAY, 2)] * 5, PYPI_RETRY_SECONDS="15")
    assert result.returncode == 2
    assert sleeps == [5, 10]
    assert len(calls) == 3
    assert LABEL in result.stderr


@pytest.mark.parametrize(
    "error",
    [
        "  Caused by: HTTP status client error (404 Not Found) for url (https://pypi.org/simple/nope/)\n",
        "  × No solution found when resolving dependencies:\n",
        "",
    ],
    ids=["not-found", "resolution", "silent"],
)
def test_a_failure_that_is_not_an_index_outage_fails_at_once(tmp_path, error):
    result, calls, sleeps, summary = _retry(tmp_path, [(error, 1), ("", 0)])
    assert result.returncode == 1
    assert len(calls) == 1
    assert sleeps == []
    assert LABEL not in result.stdout + result.stderr
    assert summary == ""


class _BadGateway(BaseHTTPRequestHandler):
    hits = 0

    def do_GET(self):
        type(self).hits += 1
        self.send_response(502)
        self.end_headers()

    def log_message(self, *args):
        pass


def _closed_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _uv():
    return shutil.which("uv") or str(Path(sys.executable).parent / "uv")


@pytest.mark.parametrize("index", ["bad-gateway", "refused"])
def test_a_real_uv_install_against_an_unreachable_index_retries_then_names_the_outage(tmp_path, index):
    server = None
    if index == "bad-gateway":
        server = ThreadingHTTPServer(("127.0.0.1", 0), _BadGateway)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/simple"
    else:
        url = f"http://127.0.0.1:{_closed_port()}/simple"
    (tmp_path / "requirements.in").write_text("setuptools\n")
    try:
        result, sleeps, summary = _run(
            tmp_path,
            _bin(tmp_path),
            [_uv(), "pip", "compile", "requirements.in", "--no-cache", "--default-index", url, "-q"],
            UV_HTTP_RETRIES="0",
        )
    finally:
        if server:
            server.shutdown()
            server.server_close()
    assert result.returncode == 2, result.stdout + result.stderr
    assert sum(sleeps) >= 120
    assert result.stderr.count("::warning title=Python package index retry::") == 7
    assert LABEL in result.stderr
    assert "not a code verdict" in result.stderr.split(LABEL)[1]
    assert "Python package index unavailable" in summary
    if server:
        assert _BadGateway.hits >= 8


def _install_steps():
    jobs = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    return {
        name: next(step for step in jobs[name]["steps"] if step.get("name") == "Install dependencies")
        for name in ("unit", "unit-selected", "test-count", "split")
    }


@pytest.mark.parametrize("job", ["unit", "unit-selected", "test-count", "split"])
def test_every_unit_dependency_install_runs_uv_through_the_retry(job):
    run = _install_steps()[job]["run"]
    install = next(line for line in run.splitlines() if "uv pip install" in line)
    assert install.strip().startswith('bash .github/scripts/pypi-retry.sh "Install dependencies" -- uv pip install')
