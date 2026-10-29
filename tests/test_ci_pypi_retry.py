import os
import shutil
import socket
import subprocess
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import distribution
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
RETRY = ROOT / ".github/scripts/pypi-retry.sh"
LABEL = "::error title=Python package index unavailable::"
BAD_GATEWAY = "  cause: HTTP status server error (502 Bad Gateway) for url (https://pypi.org/simple/setuptools/)\n"


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
        "  cause: HTTP status server error (503 Service Unavailable) for url (https://pypi.org/simple/pytest/)\n",
        "  cause: HTTP status client error (408 Request Timeout) for url (https://pypi.org/simple/pytest/)\n",
        "  cause: HTTP status client error (429 Too Many Requests) for url (https://pypi.org/simple/pytest/)\n",
        "  cause: tcp connect error: Connection refused (os error 111)\n",
        "  cause: Connection reset by peer (os error 104)\n",
        "  cause: dns error: failed to lookup address information\n",
        "  cause: operation timed out\n",
        "  cause: deadline has elapsed\n",
        "  Caused by: HTTP status server error (502 Bad Gateway) for url (https://pypi.org/simple/setuptools/)\n",
    ],
    ids=[
        "bad-gateway",
        "unavailable",
        "request-timeout",
        "too-many",
        "refused",
        "reset",
        "dns",
        "timeout",
        "deadline",
        "caused-by",
    ],
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
    assert "Install dependencies failed after 8 attempts and 155s of backoff" in message
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
        "  cause: error sending request for url (https://pypi.org/simple/pytest/)\n",
        "      Build step timed out: Connection refused by the test server\n",
        "",
    ],
    ids=["not-found", "resolution", "send-only", "build-output", "silent"],
)
def test_a_failure_that_is_not_an_index_outage_fails_at_once(tmp_path, error):
    result, calls, sleeps, summary = _retry(tmp_path, [(error, 1), ("", 0)])
    assert result.returncode == 1
    assert len(calls) == 1
    assert sleeps == []
    assert LABEL not in result.stdout + result.stderr
    assert summary == ""


def test_a_command_without_the_separator_is_never_run(tmp_path):
    bin_dir = _fake(tmp_path, [("", 0)])
    result = subprocess.run(
        ["bash", str(RETRY), "Install dependencies", "x", "install", "-e", "."],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert not (tmp_path / "calls").exists()


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
    online = [line.strip() for line in run.splitlines() if "uv pip install" in line and "--offline" not in line]
    assert online
    for install in online:
        assert install.startswith('bash .github/scripts/pypi-retry.sh "Install dependencies" -- uv pip install')


@pytest.mark.parametrize("job", ["unit", "unit-selected", "test-count", "split"])
def test_every_unit_install_tries_the_runner_wheelhouse_offline_before_pypi(job):
    lines = [line.strip() for line in _install_steps()[job]["run"].splitlines()]
    offline = next(i for i, line in enumerate(lines) if "--offline" in line)
    retry = next(i for i, line in enumerate(lines) if line.startswith("bash .github/scripts/pypi-retry.sh"))
    assert lines[offline].startswith("uv pip install --offline --no-index --find-links /opt/wheelhouse ")
    assert "exit 0" in lines[offline]
    assert offline < retry


def _mutation_installs():
    installs = {}
    for workflow, names in (
        ("test.yml", ("mutation-plan", "mutation-stats", "mutation")),
        ("mutation-preflight.yml", None),
    ):
        jobs = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())["jobs"]
        for name in names or jobs:
            for step in jobs[name].get("steps", []):
                for line in step.get("run", "").splitlines():
                    if "pip install" in line:
                        installs.setdefault((workflow, name, step["name"]), []).append(line.strip())
    return installs


def test_every_mutation_dependency_install_runs_uv_through_the_retry():
    installs = _mutation_installs()
    assert {key[:2] for key in installs} >= {
        ("test.yml", "mutation-plan"),
        ("test.yml", "mutation-stats"),
        ("test.yml", "mutation"),
        ("mutation-preflight.yml", "plan"),
        ("mutation-preflight.yml", "touched-tests"),
        ("mutation-preflight.yml", "stats"),
        ("mutation-preflight.yml", "refine"),
        ("mutation-preflight.yml", "mutation-shard"),
    }
    helper = {
        "test.yml": 'bash "$RUNNER_TEMP/workflow/.github/scripts/pypi-retry.sh"',
        "mutation-preflight.yml": "bash .github/scripts/pypi-retry.sh",
    }
    for (workflow, job, _), lines in installs.items():
        for line in lines:
            assert line.startswith(f'{helper[workflow]} "Install dependencies" -- uv pip install --system '), (
                workflow,
                job,
                line,
            )


@pytest.mark.parametrize("job", ["mutation-plan", "mutation-stats", "mutation"])
def test_mutation_jobs_take_the_retry_helper_from_the_workflow_revision(job):
    steps = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"][job]["steps"]
    names = [step.get("name") for step in steps]
    take = steps[names.index("Take the helper scripts from the workflow revision")]
    install = steps[names.index("Install dependencies")]
    assert names.index("Take the helper scripts from the workflow revision") < names.index("Install dependencies")
    assert take["if"] == install["if"]
    assert take["run"].splitlines() == [
        'git fetch --no-tags origin "$GITHUB_SHA"',
        'mkdir -p "$RUNNER_TEMP/workflow"',
        'git archive "$GITHUB_SHA" .github/scripts .github/test-requirements.lock | tar -x -C "$RUNNER_TEMP/workflow"',
    ]


def test_a_head_that_predates_the_helper_still_runs_it_from_the_workflow_revision(tmp_path):
    origin = tmp_path / "origin"
    repo = tmp_path / "repo"
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t"}
    env["GIT_COMMITTER_EMAIL"] = "t@t"

    def git(cwd, *args):
        command = ["git", "-c", "commit.gpgsign=false", *args]
        return subprocess.run(command, cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout

    origin.mkdir()
    git(origin, "init", "-q", "-b", "dev")
    (origin / "README").write_text("old\n")
    git(origin, "add", ".")
    git(origin, "commit", "-qm", "head without the helper")
    head = git(origin, "rev-parse", "HEAD").strip()
    git(tmp_path, "clone", "-q", str(origin), str(repo))
    git(repo, "checkout", "-q", "--detach", head)
    (origin / ".github/scripts").mkdir(parents=True)
    shutil.copy(RETRY, origin / ".github/scripts/pypi-retry.sh")
    shutil.copy(ROOT / ".github/test-requirements.lock", origin / ".github/test-requirements.lock")
    git(origin, "add", ".")
    git(origin, "commit", "-qm", "workflow revision")
    workflow_sha = git(origin, "rev-parse", "HEAD").strip()
    steps = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]["mutation-plan"]["steps"]
    take = next(s for s in steps if s.get("name") == "Take the helper scripts from the workflow revision")
    install = next(s for s in steps if s.get("name") == "Install dependencies")
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    run_env = {**env, "GITHUB_SHA": workflow_sha, "RUNNER_TEMP": str(runner_temp)}

    legacy = subprocess.run(
        ["bash", "-c", 'bash .github/scripts/pypi-retry.sh "Install dependencies" -- true'],
        cwd=repo,
        env=run_env,
        capture_output=True,
        text=True,
    )
    assert legacy.returncode == 127, legacy.stderr

    subprocess.run(["bash", "-euo", "pipefail", "-c", take["run"]], cwd=repo, env=run_env, check=True)
    assert not (repo / ".github/scripts/pypi-retry.sh").exists()
    command = install["run"].split(" -- ", 1)[0] + " -- true"
    result = subprocess.run(["bash", "-c", command], cwd=repo, env=run_env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_the_test_planner_installs_its_pinned_pyyaml_with_uv_through_the_retry():
    steps = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]["test-count"]["steps"]
    (plan,) = [step for step in steps if step.get("name") == "Plan the tests whose inputs did not change"]
    assert [line.strip() for line in plan["run"].splitlines() if "pip install" in line] == [
        'bash .github/scripts/pypi-retry.sh "Install the test planner dependencies" -- '
        '"$HOME/venv/bin/uv" pip install --python "$PYTHON_PATH" --quiet PyYAML==6.0.3'
    ]
    assert (
        'tail -n +1 -f --pid="$!" "$RUNNER_TEMP/plan.log"'
        in next(s for s in steps if s.get("name") == "Wait for the test plan")["run"]
    )


class _FlakyIndex(BaseHTTPRequestHandler):
    failures = 0
    wheel = None

    def do_GET(self):
        if type(self).failures:
            type(self).failures -= 1
            self.send_response(502)
            self.end_headers()
            return
        name = self.wheel.name
        if self.path.rstrip("/") == "/simple/pyyaml":
            body, kind = f'<a href="/files/{name}">{name}</a>'.encode(), "text/html"
        elif self.path == f"/files/{name}":
            body, kind = self.wheel.read_bytes(), "application/octet-stream"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command == "GET":
            self.wfile.write(body)

    do_HEAD = do_GET

    def log_message(self, *args):
        pass


def _pyyaml_wheel(directory):
    package = distribution("PyYAML")
    tag = next(
        line.removeprefix("Tag: ") for line in package.read_text("WHEEL").splitlines() if line.startswith("Tag: ")
    )
    wheel = directory / f"pyyaml-{package.version}-{tag}.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for entry in package.files:
            if ".." not in entry.parts:
                archive.write(package.locate_file(entry), str(entry))
    return wheel, package.version


def test_a_real_uv_install_retries_a_transient_index_failure_then_installs_the_package(tmp_path):
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    wheel, version = _pyyaml_wheel(wheels)
    _FlakyIndex.wheel, _FlakyIndex.failures = wheel, 2
    venv = tmp_path / "venv"
    subprocess.run([_uv(), "venv", "--python", sys.executable, str(venv)], check=True, capture_output=True, timeout=60)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FlakyIndex)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result, sleeps, summary = _run(
            tmp_path,
            _bin(tmp_path),
            [
                _uv(),
                "pip",
                "install",
                "--python",
                str(venv / "bin/python"),
                "--no-cache",
                "--default-index",
                f"http://127.0.0.1:{server.server_port}/simple",
                f"PyYAML=={version}",
            ],
            UV_HTTP_RETRIES="0",
        )
    finally:
        server.shutdown()
        server.server_close()
    assert result.returncode == 0, result.stdout + result.stderr
    assert sleeps == [5, 10]
    assert result.stderr.count("::warning title=Python package index retry::") == 2
    assert LABEL not in result.stderr
    assert summary == ""
    imported = subprocess.run(
        [str(venv / "bin/python"), "-I", "-c", "import yaml; print(yaml.__version__)"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert imported.stdout.strip() == version, imported.stdout + imported.stderr
