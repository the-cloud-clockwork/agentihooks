import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

EXERCISE = """
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from hooks import telemetry
from hooks.observability import otel

otel.FLUSH_TIMEOUT_SEC = 30


def test_every_exporter():
    telemetry.emit_log("suite.log", {"session_id": "s"})
    telemetry._http_fallback("suite.span", {}, 1.0)
    otel.get_meter()
    otel._init_done.wait(5)
    otel.record_gauge("suite.gauge", 1.0)
    otel.emit_event("suite.event")
    otel.flush()
    exporter = otel.langfuse_exporter()
    if exporter is not None:
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        provider.get_tracer("suite").start_span("suite.trace").end()
"""


@pytest.fixture
def collector():
    requests = []
    arrived = threading.Condition()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            with arrived:
                requests.append(self.path)
                arrived.notify_all()
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", requests, arrived
    server.shutdown()


def _run_suite(tmp_path, endpoint, *plugins):
    (tmp_path / "test_exercise.py").write_text(EXERCISE)
    env = {
        **os.environ,
        "AGENTIHOOKS_HOME": str(tmp_path / "home"),
        "AGENTIHOOKS_OTLP_ENDPOINT": endpoint,
        "AGENTIHOOKS_OTLP_PROTOCOL": "http/protobuf",
        "AGENTIHOOKS_LANGFUSE_ENABLED": "true",
        "OTEL_LANGFUSE_ENDPOINT": f"{endpoint}/api/public/otel",
        "LANGFUSE_PUBLIC_KEY": "fake-public",
        "LANGFUSE_SECRET_KEY": "fake-secret",
    }
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(REPO), *plugins]
    result = subprocess.run(
        [*command, str(tmp_path / "test_exercise.py")], cwd=REPO, env=env, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_exercise_reaches_the_collector_without_the_suite_fixtures(tmp_path, collector):
    endpoint, requests, arrived = collector
    _run_suite(tmp_path, endpoint)
    expected = {"/v1/logs", "/v1/traces", "/v1/metrics", "/api/public/otel/v1/traces"}
    with arrived:
        assert arrived.wait_for(lambda: expected <= set(requests), timeout=30), requests


def test_suite_run_with_collector_settings_in_the_shell_makes_no_export_call(tmp_path, collector):
    endpoint, requests, arrived = collector
    _run_suite(tmp_path, endpoint, "-p", "tests.conftest")
    with arrived:
        assert requests == []
