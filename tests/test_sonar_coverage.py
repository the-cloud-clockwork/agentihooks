import importlib.util
import os
import shlex
import subprocess
import sys
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_sonar_uses_all_shards_without_running_tests_again():
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    jobs = workflow["jobs"]
    scan = jobs["sonar"]
    assert "needs" not in scan
    merge = next(step for step in scan["steps"] if step.get("name") == "Merge shard coverage")
    assert "pytest" not in merge["run"]
    assert "combine.sh" in merge["run"]
    assert merge["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"
    assert "sonar" in jobs["gate-required"]["needs"]
    assert not (ROOT / ".github/workflows/sonar-scan.yml").exists()


def test_sonar_setup_overlaps_the_shards_and_only_the_analysis_waits_for_coverage():
    steps = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]["sonar"]["steps"]
    names = [step.get("name") or step.get("uses") for step in steps]
    merge = names.index("Merge shard coverage")
    for setup in (
        "actions/checkout@v4",
        "actions/setup-python@v5",
        "Install coverage",
        "Start Cloudflare Access proxy",
    ):
        assert names.index(setup) < merge
    assert names.index("Restore Sonar downloads") < merge < names.index("SonarQube Scan")
    combine = (ROOT / ".github/coverage/combine.sh").read_text()
    assert "collect.py" in combine
    assert "gh run download" not in combine


def test_coverage_options_measure_hooks_and_scripts_on_one_interpreter():
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    steps = workflow["jobs"]["unit"]["steps"]
    run = next(step for step in steps if step.get("name") == "Run tests")
    options = run["env"]["PYTEST_ADDOPTS"]
    assert "matrix.python-version == '3.12'" in options
    assert "--cov=hooks" in options
    assert "--cov=scripts" in options
    assert "-p no:pytest_cov" not in run["run"]
    upload = next(step for step in steps if step.get("name") == "Upload coverage")
    assert "matrix.python-version == '3.12'" in upload["if"]
    assert upload["with"]["include-hidden-files"] is True
    assert upload["with"]["if-no-files-found"] == "error"
    properties = (ROOT / "sonar-project.properties").read_text()
    assert "sonar.coverage.exclusions=scripts/**" not in properties


def test_missing_shard_coverage_is_red(tmp_path):
    for shard in (1, 2, 3):
        folder = tmp_path / ".coverage-shards" / f"coverage-3.12-{shard}"
        folder.mkdir(parents=True)
        (folder / ".coverage").write_text("plant")
    result = subprocess.run(
        ["bash", str(ROOT / ".github/coverage/combine.sh"), "--downloaded", "4"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Missing coverage for shard 4" in result.stdout


def _stub_combine(tmp_path, collect_body):
    folder = tmp_path / ".github/coverage"
    folder.mkdir(parents=True)
    for name in ("combine.sh", "coverage.ini"):
        (folder / name).write_text((ROOT / ".github/coverage" / name).read_text())
    (folder / "collect.py").write_text(f"import sys\nprint('collect', *sys.argv[1:])\n{collect_body}\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text("#!/usr/bin/env bash\necho 555\n")
    (bin_dir / "gh").chmod(0o755)
    (bin_dir / "python").symlink_to(sys.executable)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "t"],
        cwd=tmp_path,
        check=True,
    )
    return folder / "combine.sh", dict(
        os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", GITHUB_REPOSITORY="owner/repo"
    )


@pytest.mark.parametrize(("event", "source"), [("push", "555"), ("pull_request", "42")])
def test_combine_collects_from_the_passed_run_on_push_and_this_run_otherwise(tmp_path, event, source):
    script, env = _stub_combine(tmp_path, "sys.exit(3)")
    result = subprocess.run(
        ["bash", str(script), "42", "8"],
        cwd=tmp_path,
        env=dict(env, GITHUB_EVENT_NAME=event),
        capture_output=True,
        text=True,
    )
    assert f"collect {source} 8 .coverage-shards" in result.stdout
    assert result.returncode != 0
    assert not (tmp_path / "coverage.xml").exists()


def test_combined_coverage_keeps_hits_from_every_shard_and_both_packages(tmp_path):
    for package in ("hooks", "scripts"):
        (tmp_path / package).mkdir()
        for shard in range(1, 5):
            (tmp_path / package / f"part_{shard}.py").write_text("value = 1\n")
    generator = """
import runpy
import sys
from coverage import Coverage
from pathlib import Path
for shard in range(1, 5):
    folder = Path('.coverage-shards') / f'coverage-3.12-{shard}'
    folder.mkdir(parents=True)
    cov = Coverage(data_file=str(folder / '.coverage'), config_file=sys.argv[1])
    cov.start()
    for package in ('hooks', 'scripts'):
        runpy.run_path(f'{package}/part_{shard}.py')
    cov.stop()
    cov.save()
"""
    subprocess.run(
        [sys.executable, "-c", generator, str(ROOT / ".github/coverage/coverage.ini")],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    result = subprocess.run(
        ["bash", str(ROOT / ".github/coverage/combine.sh"), "--downloaded", "4"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "TOTAL" in result.stdout
    report = ET.parse(tmp_path / "coverage.xml")
    classes = report.findall(".//class")
    assert {node.attrib["filename"] for node in classes} == {
        f"{package}/part_{shard}.py" for package in ("hooks", "scripts") for shard in range(1, 5)
    }
    assert all(int(line.attrib["hits"]) > 0 for line in report.findall(".//line"))


def test_coverage_options_do_not_reach_nested_test_runners(tmp_path):
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    step = next(step for step in workflow["jobs"]["unit"]["steps"] if step.get("name") == "Run tests")
    invocation = shlex.split(step["run"].split(" tests/", 1)[0])
    invocation[0] = sys.executable
    fixture = tmp_path / "test_environment.py"
    fixture.write_text("import os\ndef test_nested_runner_environment():\n    assert not os.getenv('PYTEST_ADDOPTS')\n")
    result = subprocess.run(
        [*invocation, str(fixture)],
        cwd=tmp_path,
        env={**os.environ, "PYTEST_ADDOPTS": "-q"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("method,status", [("GET", 200), ("POST", 201), ("GET", 403)])
def test_proxy_forwards_headers_uploads_and_backend_status(monkeypatch, method, status):
    spec = importlib.util.spec_from_file_location("sonar_proxy", ROOT / ".github/coverage/proxy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    received = []

    class Backend(BaseHTTPRequestHandler):
        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            received.append((self.command, self.path, dict(self.headers), body))
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b"backend result")

        def log_message(self, format, *args):
            pass

    with (
        ThreadingHTTPServer(("127.0.0.1", 0), Backend) as backend,
        ThreadingHTTPServer(("127.0.0.1", 0), module.SonarProxy) as proxy,
    ):
        monkeypatch.setenv("SONAR_HOST_URL", f"http://127.0.0.1:{backend.server_port}")
        monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "fixture")
        monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", "fixture")
        threads = [threading.Thread(target=server.serve_forever) for server in (backend, proxy)]
        for thread in threads:
            thread.start()
        try:
            body = b"analysis payload" if method == "POST" else None
            request = Request(
                f"http://127.0.0.1:{proxy.server_port}/api/ce/submit?projectKey=fixture",
                data=body,
                headers={"Authorization": "fixture", "Content-Type": "application/octet-stream"},
                method=method,
            )
            try:
                response = urlopen(request, timeout=5)
            except HTTPError as error:
                response = error
            with response:
                assert response.status == status
                assert response.read() == b"backend result"
            assert received[0][0:2] == (method, "/api/ce/submit?projectKey=fixture")
            headers = {name.lower(): value for name, value in received[0][2].items()}
            assert headers["cf-access-client-id"] == "fixture"
            assert headers["cf-access-client-secret"] == "fixture"
            assert headers["authorization"] == "fixture"
            assert received[0][3] == (body or b"")
        finally:
            backend.shutdown()
            proxy.shutdown()
            for thread in threads:
                thread.join(timeout=5)
                assert not thread.is_alive()


def test_proxy_rejects_redirect_without_forwarding_credentials(monkeypatch):
    spec = importlib.util.spec_from_file_location("sonar_proxy", ROOT / ".github/coverage/proxy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    received = []

    class Sink(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(dict(self.headers))
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{sink.server_port}/")
            self.end_headers()

        def log_message(self, format, *args):
            pass

    with (
        ThreadingHTTPServer(("127.0.0.1", 0), Sink) as sink,
        ThreadingHTTPServer(("127.0.0.1", 0), Redirect) as backend,
        ThreadingHTTPServer(("127.0.0.1", 0), module.SonarProxy) as proxy,
    ):
        monkeypatch.setenv("SONAR_HOST_URL", f"http://127.0.0.1:{backend.server_port}")
        monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "fixture")
        monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", "fixture")
        servers = (sink, backend, proxy)
        threads = [threading.Thread(target=server.serve_forever) for server in servers]
        for thread in threads:
            thread.start()
        try:
            request = Request(f"http://127.0.0.1:{proxy.server_port}/", headers={"Authorization": "fixture"})
            try:
                response = urlopen(request, timeout=5)
            except HTTPError as error:
                response = error
            with response:
                assert response.status == 502
            assert received == []
        finally:
            for server in servers:
                server.shutdown()
            for thread in threads:
                thread.join(timeout=5)
                assert not thread.is_alive()
