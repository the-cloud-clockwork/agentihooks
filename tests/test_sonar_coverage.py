import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
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
    assert scan["needs"] == ["unit", "reuse"]
    merge = next(step for step in scan["steps"] if step.get("name") == "Merge shard coverage")
    assert "pytest" not in merge["run"]
    assert "combine.sh" in merge["run"]
    assert "env" not in merge
    assert "sonar" in jobs["gate-required"]["needs"]
    assert not (ROOT / ".github/workflows/sonar-scan.yml").exists()


def test_sonar_sets_up_before_merging_coverage():
    steps = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]["sonar"]["steps"]
    names = [step.get("name") or step.get("uses") for step in steps]
    merge = names.index("Merge shard coverage")
    for setup in ("actions/checkout@v4", "actions/setup-python@v5", "Install coverage"):
        assert names.index(setup) < merge
    assert merge < names.index("Wait for the coverage merge") < names.index("SonarQube Scan")


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


@pytest.mark.parametrize("args", [["42", "8"], []], ids=["run-id", "no-arguments"])
def test_combine_refuses_anything_but_downloaded_coverage(tmp_path, args):
    result = subprocess.run(
        ["bash", str(ROOT / ".github/coverage/combine.sh"), *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "::error::Usage: combine.sh --downloaded <shards>" in result.stdout
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
(folder / 'js-coverage').mkdir()
(folder / 'js-coverage' / 'sources').mkdir(parents=True)
(folder / 'js-coverage' / 'sources' / 's.js').write_text('void 0;\\n')
for kind in ('node', 'browser'):
    (folder / 'js-coverage' / f'capture-{kind}-1.json').write_text('{"result": [{"source": "s", "functions": [{"ranges": [{"startOffset": 0, "endOffset": 7, "count": 1}]}]}]}')
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
    assert (tmp_path / "lcov.info").exists()


_RENDEZVOUS = """
import sys
import time
from pathlib import Path

def meet(mine, theirs, failure):
    Path(mine).touch()
    deadline = time.monotonic() + 5
    while not Path(theirs).exists():
        if time.monotonic() > deadline:
            sys.exit(failure)
        time.sleep(0.05)
"""


def _stubbed_combine_tree(tmp_path, lcov_exit=0, xml_exit=0, lcov_late=0):
    shutil.copytree(ROOT / ".github/coverage", tmp_path / ".github/coverage")
    for shard in range(1, 3):
        report = tmp_path / ".coverage-shards" / f"coverage-3.12-{shard}" / ".coverage"
        report.parent.mkdir(parents=True)
        report.write_text("data")
    (tmp_path / "coverage").mkdir()
    (tmp_path / "coverage/__init__.py").write_text("")
    (tmp_path / "coverage/__main__.py").write_text(
        _RENDEZVOUS
        + f"""
if sys.argv[1] == "xml":
    meet("xml.started", "lcov.started", "the JS conversion never ran beside the XML")
    if {xml_exit}:
        sys.exit({xml_exit})
    Path("coverage.xml").write_text("<coverage/>")
elif sys.argv[1] == "report":
    print("TOTAL")
"""
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/js_lcov.py").write_text(
        _RENDEZVOUS
        + f"""
meet("lcov.started", "xml.started", "the XML never ran beside the JS conversion")
time.sleep({lcov_late})
Path("lcov.info").write_text("")
sys.exit({lcov_exit})
"""
    )
    return subprocess.run(
        ["bash", str(tmp_path / ".github/coverage/combine.sh"), "--downloaded", "2"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_the_js_conversion_runs_beside_the_python_coverage_xml(tmp_path):
    result = _stubbed_combine_tree(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "coverage.xml").exists()
    assert (tmp_path / "lcov.info").exists()
    assert "TOTAL" in result.stdout


def test_a_failed_js_conversion_fails_the_merge(tmp_path):
    result = _stubbed_combine_tree(tmp_path, lcov_exit=3)
    assert result.returncode == 3, result.stdout + result.stderr
    assert (tmp_path / "coverage.xml").exists()


def test_a_failed_python_xml_fails_the_merge_and_stops_the_js_conversion(tmp_path):
    result = _stubbed_combine_tree(tmp_path, xml_exit=4, lcov_late=2)
    assert result.returncode == 4, result.stdout + result.stderr
    assert not (tmp_path / "coverage.xml").exists()
    time.sleep(3)
    assert not (tmp_path / "lcov.info").exists()


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
