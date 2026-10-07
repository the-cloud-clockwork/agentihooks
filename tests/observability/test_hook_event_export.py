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
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            paths.append(self.path)
            if self.path == "/v1/metrics":
                from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest

                request = ExportMetricsServiceRequest.FromString(body)
                for resource in request.resource_metrics:
                    for scope in resource.scope_metrics:
                        paths.extend(scope.metrics)
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


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_native_token_measurements_reach_collector(collector, tmp_path, target):
    endpoint, paths = collector
    codex_home = tmp_path / "codex"
    sessions = codex_home / "sessions" / "2026" / "10" / "07"
    sessions.mkdir(parents=True)
    transcript = sessions / ("rollout-native-" + target + ".jsonl")
    transcript.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"last_token_usage": {"total_tokens": 50000}, "model_context_window": 200000},
                },
            }
        )
        + "\n"
    )
    env = {
        **os.environ,
        "AGENTIHOOKS_HOME": str(tmp_path / "home"),
        "AGENTIHOOKS_TARGET": target,
        "CODEX_HOME": str(codex_home),
        "AGENTIHOOKS_AGENT_NAME": "proof-life",
        "AGENTIHOOKS_PROFILE": "engineer",
        "AGENTIHOOKS_SWARM": "",
        "AGENTIHOOKS_SWARM_TASK": "",
        "AGENTIHOOKS_PROFILE_REPORT": str(tmp_path / "report.json"),
        "AGENTIHOOKS_OTLP_ENDPOINT": endpoint,
        "AGENTIHOOKS_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_HOOKS_ENABLED": "true",
        "TOKEN_MONITOR_ENABLED": "true",
    }
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "true"},
        "session_id": "native-" + target,
        "transcript_path": str(transcript),
    }
    (tmp_path / "report.json").write_text(
        json.dumps(
            {
                "state": "validated",
                "validation": {"profile": "engineer"},
            }
        )
    )
    script = _HOOK_WITH_LOAD_PROOF_FLUSH
    if target == "claude":
        payload["context_window"] = {"context_window_size": 200000, "used_percentage": 25}
        script = "from hooks.statusline import main; main()"
    result = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=_PROJECT_ROOT,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    gauges = [
        metric for metric in paths if not isinstance(metric, str) and metric.name == "agentihooks.tokens.fill_pct"
    ]
    assert gauges
    point = gauges[0].gauge.data_points[0]
    assert point.as_double == 25.0
    attributes = {item.key: item.value.string_value for item in point.attributes}
    from hooks.observability.agent_trace import trace_id

    assert attributes["session.id"] == "native-" + target
    assert attributes["agentihooks.correlation.session.id"] == "native-" + target
    assert attributes["agentihooks.correlation.trace.id"] == format(trace_id("native-" + target), "032x")
    assert attributes["agentihooks.correlation.profile.resolved"] == "engineer"
    assert attributes["agentihooks.correlation.agent.name"] == "proof-life"
    assert attributes["agentihooks.correlation.agent.life.state"] == "unsupported"
    assert attributes["agentihooks.correlation.seat.state"] == "unsupported"


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
