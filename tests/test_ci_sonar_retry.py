import importlib.util
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
RETRY = ROOT / ".github/coverage/sonar-retry.sh"
LABEL = "::error title=Sonar server unavailable::"


def _sonar_steps():
    return yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]["sonar"]["steps"]


def _fake(tmp_path, answers):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    queue = tmp_path / "answers"
    queue.mkdir(exist_ok=True)
    for index, (out, err, status) in enumerate(answers):
        (queue / f"{index:03}.out").write_text(out)
        (queue / f"{index:03}.err").write_text(err)
        (queue / f"{index:03}.status").write_text(str(status))
    command = bin_dir / "sonar-call"
    command.write_text(
        "#!/usr/bin/env bash\n"
        f'n=$(ls "{queue}"/*.status | head -n1); base="${{n%.status}}"\n'
        'cat "$base.out"; cat "$base.err" >&2; status=$(cat "$n")\n'
        f'echo "$*" >> "{tmp_path}/calls"\n'
        'rm "$base.out" "$base.err" "$n"\n'
        'exit "$status"\n'
    )
    command.chmod(0o755)
    (bin_dir / "sleep").write_text(f'#!/usr/bin/env bash\necho "$1" >> "{tmp_path}/sleeps"\n')
    (bin_dir / "sleep").chmod(0o755)
    return bin_dir


def _retry(tmp_path, answers, *options, seconds="180"):
    bin_dir = _fake(tmp_path, answers)
    summary = tmp_path / "summary"
    result = subprocess.run(
        ["bash", str(RETRY), *options, "Sonar status", "--", "sonar-call", "http://127.0.0.1:9000/api/x"],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "SONAR_RETRY_SECONDS": seconds,
            "GITHUB_STEP_SUMMARY": str(summary),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    calls = (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []
    sleeps = [int(s) for s in (tmp_path / "sleeps").read_text().split()] if (tmp_path / "sleeps").exists() else []
    return result, calls, sleeps, summary.read_text() if summary.exists() else ""


FORBIDDEN = ("", "curl: (22) The requested URL returned error: 403\n", 22)


@pytest.mark.parametrize(
    "failure",
    [
        ("", "curl: (7) Failed to connect to 127.0.0.1 port 9000 after 0 ms: Couldn't connect to server\n", 7),
        ("", "curl: (22) The requested URL returned error: 502\n", 22),
        ("", "curl: (28) Operation timed out after 10002 milliseconds with 0 bytes received\n", 28),
        ("partial", "curl: (52) Empty reply from server\n", 52),
        (
            "ERROR Error during SonarScanner Engine execution\nCaused by: java.io.IOException: unexpected end of stream\n",
            "",
            3,
        ),
        ("Call to URL [http://127.0.0.1:9000/api/v2/analysis/engine] failed: timeout\n", "", 1),
        ("ERROR Failed to download analysis cache: HTTP code 502: <html>Bad gateway</html>\n", "", 3),
    ],
    ids=["refused", "bad-gateway", "curl-timeout", "empty-reply", "scanner-eof", "engine-timeout", "analysis-cache"],
)
def test_a_transient_server_failure_is_retried_with_backoff_and_only_the_answer_reaches_stdout(tmp_path, failure):
    result, calls, sleeps, summary = _retry(tmp_path, [failure, failure, ('{"status":"UP"}', "", 0)])
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == '{"status":"UP"}'
    assert len(calls) == 3
    assert sleeps == [2, 4]
    assert result.stderr.count("::warning title=Sonar server retry::") == 2
    assert LABEL not in result.stdout + result.stderr
    assert summary == ""


def test_a_server_that_stays_down_ends_red_with_the_infrastructure_label(tmp_path):
    result, calls, sleeps, summary = _retry(
        tmp_path, [("", "curl: (22) The requested URL returned error: 502\n", 22)] * 20, seconds="60"
    )
    assert result.returncode == 22
    assert sum(sleeps) <= 60
    assert sleeps == [2, 4, 8, 16, 30]
    assert len(calls) == 6
    assert LABEL in result.stderr
    assert "Sonar status" in result.stderr
    assert "returned error: 502" in result.stderr.split(LABEL)[1]
    assert "not a code verdict" in result.stderr
    assert "Sonar server unavailable" in summary
    assert "Quality Gate" not in result.stdout + result.stderr


@pytest.mark.parametrize("code", [401, 403])
@pytest.mark.parametrize(
    "message",
    [
        "curl: (22) The requested URL returned error: {code}",
        "Error status returned by url [http://127.0.0.1:9000/api/x]: {code}",
        "Call to URL [http://127.0.0.1:9000/api/x] failed with HTTP {code}",
        "ERROR Failed to download analysis cache: HTTP code {code}: Unauthorized",
    ],
)
def test_a_repeated_authorization_failure_stops_after_one_short_retry(tmp_path, code, message):
    failure = ("", message.format(code=code) + "\n", 22)
    result, calls, sleeps, summary = _retry(tmp_path, [failure] * 20)
    assert result.returncode == 22
    assert len(calls) == 2
    assert sleeps == [2]
    assert LABEL in result.stderr
    assert str(code) in result.stderr.split(LABEL)[1]
    assert str(code) in summary


@pytest.mark.parametrize("code", [401, 403])
def test_a_brief_authorization_failure_can_recover_on_the_single_retry(tmp_path, code):
    failure = ("", f"curl: (22) The requested URL returned error: {code}\n", 22)
    result, calls, sleeps, summary = _retry(tmp_path, [failure, ('{"status":"UP"}', "", 0)])
    assert result.returncode == 0
    assert result.stdout == '{"status":"UP"}'
    assert len(calls) == 2
    assert sleeps == [2]
    assert LABEL not in result.stderr
    assert summary == ""


@pytest.mark.parametrize("name", ["Hold the Delivery L2 binding", "Hold the Delivery L2 conditions"])
def test_the_dependent_checks_require_successful_proxy_startup(name):
    steps = {step.get("name"): step for step in _sonar_steps()}
    assert "steps.proxy.outcome == 'success'" in steps[name]["if"]
    if name == "Hold the Delivery L2 binding":
        assert "success()" in steps[name]["if"]
    else:
        assert "always()" in steps[name]["if"]


@pytest.mark.parametrize(
    "failure",
    [
        ("", "curl: (22) The requested URL returned error: 404\n", 22),
        ("", "", 22),
        ("ERROR Quality profile not found\n", "", 2),
        ("ERROR Failed to download analysis cache: HTTP code 404: Not Found\n", "", 3),
    ],
    ids=["not-found", "silent", "scanner-config", "analysis-cache-not-found"],
)
def test_a_failure_that_is_not_the_server_fails_at_once_without_the_label(tmp_path, failure):
    result, calls, sleeps, _ = _retry(tmp_path, [failure, ('{"status":"UP"}', "", 0)])
    assert result.returncode == failure[2]
    assert len(calls) == 1
    assert sleeps == []
    assert LABEL not in result.stdout + result.stderr


@pytest.mark.parametrize("code", [500, 502, 503, 599])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_a_scanner_upload_server_error_recovers_on_one_retry(tmp_path, code, stream):
    error = f"java.lang.IllegalStateException: Failed to upload report: Error {code} on http://127.0.0.1:9000/api/ce/submit?projectKey=agentihooks : Bad gateway\n"
    failure = (error, "", 1) if stream == "stdout" else ("", error, 1)
    result, calls, sleeps, summary = _retry(
        tmp_path, [failure, ("ANALYSIS SUCCESSFUL\n", "", 0)], "--unless", ".scannerwork/report-task.txt"
    )
    assert result.returncode == 0
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert sleeps == [2]
    assert result.stdout == "ANALYSIS SUCCESSFUL\n"
    assert "::warning title=Sonar server retry::" in result.stderr
    assert LABEL not in result.stderr
    assert summary == ""


@pytest.mark.parametrize(
    "second_error",
    [
        "Failed to upload report: Error 502 on http://localhost/api/ce/submit",
        "Connection reset",
        "ERROR Quality profile not found",
        "",
    ],
)
def test_an_upload_retry_never_leads_to_a_third_scan_when_the_failure_changes(tmp_path, second_error):
    error = "Failed to upload report: Error 502 on http://localhost/api/ce/submit\nConnection reset\n"
    result, calls, sleeps, summary = _retry(
        tmp_path, [(error, "", 1), ("", second_error, 8), ("", "", 0)], "--unless", ".scannerwork/report-task.txt"
    )
    assert result.returncode == 8
    assert len(calls) == 2
    assert sleeps == [2]
    assert LABEL in result.stderr
    assert "Sonar server outage (infrastructure)" in summary


@pytest.mark.parametrize("seconds, count", [("180", 2), ("1", 1)])
def test_a_scanner_upload_outage_stays_red_after_one_retry_or_its_budget(tmp_path, seconds, count):
    error = "Failed to upload report: Error 502 on http://127.0.0.1:9000/api/ce/submit?projectKey=agentihooks : Bad gateway\n"
    result, calls, sleeps, summary = _retry(
        tmp_path, [("", error, 7)] * 3, "--unless", ".scannerwork/report-task.txt", seconds=seconds
    )
    assert result.returncode == 7
    assert len(calls) == count
    assert sleeps == ([2] if count == 2 else [])
    assert LABEL in result.stderr
    assert "Sonar server outage (infrastructure)" in result.stderr
    assert "Sonar server outage (infrastructure)" in summary
    assert "Error 502" in result.stderr
    assert "Quality Gate" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "error",
    [
        "Failed to upload report: Error 400 on http://localhost/api/ce/submit",
        "Failed to upload report: Error 404 on http://localhost/api/ce/submit",
        "Failed to upload report: Error 403 on http://localhost/api/ce/submit",
        "Failed to upload report: Error 502 on http://localhost/api/other",
        "Error 502 on http://localhost/api/ce/submit",
    ],
)
def test_other_scanner_errors_are_not_classified_as_an_upload_outage(tmp_path, error):
    result, calls, sleeps, summary = _retry(
        tmp_path, [(error, "", 3), ("ANALYSIS SUCCESSFUL", "", 0)], "--unless", ".scannerwork/report-task.txt"
    )
    assert result.returncode == 3
    assert len(calls) == 1
    assert sleeps == []
    assert LABEL not in result.stderr
    assert summary == ""


def test_an_accepted_report_prevents_a_retry_even_with_an_upload_server_error(tmp_path):
    (tmp_path / ".scannerwork").mkdir()
    (tmp_path / ".scannerwork/report-task.txt").write_text("ceTaskId=T1\n")
    error = "Failed to upload report: Error 502 on http://localhost/api/ce/submit"
    result, calls, sleeps, summary = _retry(
        tmp_path, [(error, "", 4), ("", "", 0)], "--unless", ".scannerwork/report-task.txt"
    )
    assert result.returncode == 4
    assert len(calls) == 1
    assert sleeps == []
    assert LABEL not in result.stderr
    assert summary == ""


def test_a_scanner_that_already_submitted_its_report_is_never_run_again(tmp_path):
    (tmp_path / ".scannerwork").mkdir()
    (tmp_path / ".scannerwork/report-task.txt").write_text("ceTaskId=T1\n")
    scanner_eof = ("Caused by: java.io.IOException: unexpected end of stream\n", "", 1)
    result, calls, sleeps, _ = _retry(tmp_path, [scanner_eof, ("", "", 0)], "--unless", ".scannerwork/report-task.txt")
    assert result.returncode == 1
    assert len(calls) == 1
    assert sleeps == []


def test_every_sonar_server_call_goes_through_the_retry():
    steps = {step.get("name"): step for step in _sonar_steps()}
    for name in (
        "Start Cloudflare Access proxy",
        "Hold the Delivery L2 binding",
        "SonarQube Scan",
        "SonarQube Quality Gate",
        "Hold the Delivery L2 conditions",
    ):
        assert '"$GITHUB_WORKSPACE/.github/coverage/sonar-retry.sh"' in steps[name]["run"], name
    assert "{1..5}" not in steps["Start Cloudflare Access proxy"]["run"]
    assert "--retry" not in steps["SonarQube Quality Gate"]["run"]
    assert "--unless .scannerwork/report-task.txt" in steps["SonarQube Scan"]["run"]


def test_the_proxy_log_is_shown_when_a_sonar_step_fails():
    steps = _sonar_steps()
    shown = next(step for step in steps if step.get("name") == "Show the Sonar proxy log")
    stop = next(step for step in steps if step.get("name") == "Stop the proxy")
    assert shown["if"] == "failure() && steps.proxy.outputs.pid != ''"
    assert shown["run"].strip() == "cat sonar-proxy.log"
    assert steps.index(shown) < steps.index(stop)


def _proxy_module():
    spec = importlib.util.spec_from_file_location("sonar_proxy", ROOT / ".github/coverage/proxy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _through_proxy(monkeypatch, capsys, backend_handler, upstream=None):
    module = _proxy_module()
    monkeypatch.setattr(module, "UPSTREAM_TIMEOUT", 0.5)
    servers = [ThreadingHTTPServer(("127.0.0.1", 0), backend_handler)] if backend_handler else []
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), module.SonarProxy)
    servers.append(proxy)
    host = upstream or f"http://127.0.0.1:{servers[0].server_port}"
    monkeypatch.setenv("SONAR_HOST_URL", host)
    monkeypatch.setenv("CF_ACCESS_CLIENT_ID", "client-id-fixture")
    monkeypatch.setenv("CF_ACCESS_CLIENT_SECRET", "client-secret-fixture")
    threads = [threading.Thread(target=server.serve_forever) for server in servers]
    for thread in threads:
        thread.start()
    try:
        try:
            response = urlopen(f"http://127.0.0.1:{proxy.server_port}/api/system/status", timeout=5)
        except HTTPError as error:
            response = error
        with response:
            status = response.status
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)
    return status, capsys.readouterr().err


def test_an_upstream_timeout_is_answered_with_504_and_logged(monkeypatch, capsys):
    class Slow(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(2)

        def log_message(self, format, *args):
            pass

    status, log = _through_proxy(monkeypatch, capsys, Slow)
    assert status == 504
    assert "GET /api/system/status" in log
    assert "client-secret-fixture" not in log


def test_an_unreachable_upstream_is_answered_with_502_and_logged(monkeypatch, capsys):
    status, log = _through_proxy(monkeypatch, capsys, None, upstream="http://127.0.0.1:9")
    assert status == 502
    assert "GET /api/system/status" in log


def test_a_refused_upstream_answer_is_logged_with_who_refused(monkeypatch, capsys):
    class Forbidden(BaseHTTPRequestHandler):
        def version_string(self):
            return "cloudflare"

        def do_GET(self):
            self.send_response(403)
            self.send_header("CF-RAY", "8c1fixture-AMS")
            self.send_header("cf-mitigated", "challenge")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format, *args):
            pass

    status, log = _through_proxy(monkeypatch, capsys, Forbidden)
    assert status == 403
    line = next(line for line in log.splitlines() if "403" in line)
    for part in ("GET /api/system/status", "server=cloudflare", "cf-ray=8c1fixture-AMS", "cf-mitigated=challenge"):
        assert part in line
    assert "client-id-fixture" not in log
    assert "client-secret-fixture" not in log
