import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Under runner load the worker's SDK init alone outlasts the 1s production flush budget.
_HOOK_WITH_LOAD_PROOF_FLUSH = (
    "import runpy\n"
    "from hooks.observability import otel\n"
    "otel.FLUSH_TIMEOUT_SEC = 30\n"
    "runpy.run_module('hooks', run_name='__main__')\n"
)


@pytest.fixture
def collector():
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            paths.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", paths
    server.shutdown()


def test_hook_event_is_exported_before_the_hook_process_exits(collector, tmp_path):
    endpoint, paths = collector
    env = {
        **os.environ,
        "AGENTIHOOKS_HOME": str(tmp_path / "home"),
        "AGENTIHOOKS_OTLP_ENDPOINT": endpoint,
        "AGENTIHOOKS_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_HOOKS_ENABLED": "true",
    }
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "false"},
        "is_error": True,
        "session_id": "export-test",
        "transcript_path": "",
    }
    result = subprocess.run(
        [sys.executable, "-c", _HOOK_WITH_LOAD_PROOF_FLUSH],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=_PROJECT_ROOT,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0
    assert "/v1/logs" in paths


def test_flush_drains_every_provider(monkeypatch):
    from hooks.observability import otel

    for name in ("AGENTIHOOKS_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    flushed = []
    provider = type("Provider", (), {"force_flush": lambda self, timeout: flushed.append(self)})
    providers = [provider(), provider(), provider()]
    monkeypatch.setattr(otel, "_providers", list(providers))
    otel.init()
    otel.flush()
    assert flushed == providers
