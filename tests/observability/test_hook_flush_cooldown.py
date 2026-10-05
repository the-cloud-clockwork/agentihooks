import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


def test_offline_collector_hooks_skip_repeated_flush_waits(tmp_path):
    with socket.socket() as offline:
        offline.bind(("127.0.0.1", 0))
        endpoint = f"http://127.0.0.1:{offline.getsockname()[1]}"
        env = {
            **os.environ,
            "AGENTIHOOKS_HOME": str(tmp_path / "home"),
            "AGENTIHOOKS_OTLP_ENDPOINT": endpoint,
            "AGENTIHOOKS_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_HOOKS_ENABLED": "true",
            "CLAUDE_HOOK_LOG_ENABLED": "true",
            "CLAUDE_HOOK_LOG_FILE": str(tmp_path / "hooks.log"),
        }
        payload = json.dumps(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "false"},
                "is_error": True,
                "session_id": "offline-collector-test",
                "transcript_path": "",
            }
        )
        times = []
        for _ in range(3):
            start = time.monotonic()
            result = subprocess.run(
                [sys.executable, "-m", "hooks"],
                input=payload,
                capture_output=True,
                text=True,
                cwd=Path(__file__).resolve().parents[2],
                env=env,
                timeout=15,
            )
            times.append(time.monotonic() - start)
            assert result.returncode == 0, result.stderr
    print("offline collector hook exit seconds:", ", ".join(f"{elapsed:.3f}" for elapsed in times))
    assert max(times[1:]) < times[0] - 0.6, times
    messages = [json.loads(line)["message"] for line in (tmp_path / "hooks.log").read_text().splitlines()]
    assert messages.count("Telemetry flush failed; skipping flush waits for 60s") == 1


@pytest.mark.parametrize("failure", [False, RuntimeError("export failed")])
def test_failed_provider_flush_retries_after_cooldown(monkeypatch, failure):
    from hooks.observability import otel

    monkeypatch.setenv("AGENTIHOOKS_OTLP_ENDPOINT", "http://127.0.0.1:1")
    monkeypatch.setattr(otel, "_can_init", lambda: False)
    calls = []

    class Provider:
        def force_flush(self, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                if isinstance(failure, Exception):
                    raise failure
                return failure
            return True

    monkeypatch.setattr(otel, "_providers", [Provider()])
    otel.init()
    otel.flush()
    otel.flush()
    assert len(calls) == 1
    now = time.time()
    monkeypatch.setattr(otel.time, "time", lambda: now + 61)
    otel.flush()
    otel.flush()
    assert len(calls) == 3
