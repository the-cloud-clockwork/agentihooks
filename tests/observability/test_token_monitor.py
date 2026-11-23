"""Tests for hooks.observability.token_monitor."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import ANY, patch

import pytest

pytestmark = pytest.mark.unit

SEAM_ENV = {
    "OTEL_HOOKS_ENABLED": "true",
    "AGENTIHOOKS_OTLP_ENDPOINT": "http://127.0.0.1:9",
    "AGENTIHOOKS_OTLP_PROTOCOL": "http/protobuf",
    "TOKEN_MONITOR_ENABLED": "true",
    "REDIS_URL": "",
}


def _tokens(session_id: str) -> dict:
    from hooks.observability import presence

    path = presence.seen_dir() / f"{session_id}.json"
    return json.loads(path.read_text()).get("tokens", {}) if path.exists() else {}


def _otel_after(run: str, env: dict[str, str]) -> dict:
    from hooks import config

    probe = (
        "import json, threading\n"
        "from hooks.observability import otel\n"
        f"{run}\n"
        "worker = any(thread.name == 'otel-worker' for thread in threading.enumerate())\n"
        "print(json.dumps({'initialized': otel._initialized, 'worker': worker}))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, **SEAM_ENV, **env, "AGENTIHOOKS_HOME": str(config.AGENTIHOOKS_HOME)},
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return json.loads(done.stdout.splitlines()[-1])


def _payload(used: int, remaining: int, session_id: str = "sess-test", model: str = "sonnet-4.6") -> dict:
    return {
        "session_id": session_id,
        "model": model,
        "context_window": {"used": used, "remaining": remaining},
    }


class TestTokenMonitor:
    """Tests for context window metrics and status line generation."""

    def test_fill_pct_calculation(self):
        """used=234000, remaining=766000 → 23.4%."""
        from hooks.observability.token_monitor import get_context_fill_pct

        p = _payload(234000, 766000)
        result = get_context_fill_pct(p)
        assert result is not None
        assert abs(result - 23.4) < 0.1

    def test_fill_pct_missing_context_window(self):
        """Missing context_window → None."""
        from hooks.observability.token_monitor import get_context_fill_pct

        assert get_context_fill_pct({}) is None
        assert get_context_fill_pct({"context_window": None}) is None

    def test_below_warn_threshold(self):
        """fill_pct=50 (< 60) → (False, '')."""
        from hooks.observability.token_monitor import should_warn_context

        with patch("hooks.observability.token_monitor.get_redis", return_value=None):
            result = should_warn_context(50.0, "sess-below")
        assert result == (False, "")

    def test_at_warn_threshold(self):
        """fill_pct=60 → (True, 'warning')."""
        from hooks.observability.token_monitor import should_warn_context

        with patch("hooks.observability.token_monitor.get_redis", return_value=None):
            warn, level = should_warn_context(60.0, "sess-warn")
        assert warn is True
        assert level == "warning"

    def test_above_warn_below_critical(self):
        """fill_pct=70 → (True, 'warning')."""
        from hooks.observability.token_monitor import should_warn_context

        with patch("hooks.observability.token_monitor.get_redis", return_value=None):
            warn, level = should_warn_context(70.0, "sess-70")
        assert warn is True
        assert level == "warning"

    def test_at_critical_threshold(self):
        """fill_pct=80 → (True, 'critical')."""
        from hooks.observability.token_monitor import should_warn_context

        with patch("hooks.observability.token_monitor.get_redis", return_value=None):
            warn, level = should_warn_context(80.0, "sess-crit")
        assert warn is True
        assert level == "critical"

    def test_status_line_format(self):
        """Status line contains ctx:, %, and model name."""
        from hooks.observability.token_monitor import update_context_metrics

        p = _payload(234000, 766000, model="sonnet-4.6")
        with patch("hooks.observability.token_monitor.get_redis", return_value=None):
            result = update_context_metrics(p)
        assert "ctx:" in result
        assert "%" in result
        assert "sonnet-4.6" in result

    def test_status_line_k_format(self):
        """Used value formatted with K suffix."""
        from hooks.observability.token_monitor import update_context_metrics

        p = _payload(50_000, 950_000)
        with patch("hooks.observability.token_monitor.get_redis", return_value=None):
            result = update_context_metrics(p)
        assert "50K" in result

    def test_no_redis_graceful_fallback(self):
        """Redis unavailable → update_context_metrics still returns valid string."""
        from hooks.observability.token_monitor import update_context_metrics

        p = _payload(100_000, 900_000)
        with patch("hooks.observability.token_monitor.get_redis", return_value=None):
            result = update_context_metrics(p)
        assert isinstance(result, str)
        assert len(result) > 0
        assert "ctx:" in result

    def test_empty_payload_graceful(self):
        """Empty payload → returns placeholder string without raising."""
        from hooks.observability.token_monitor import update_context_metrics

        with patch("hooks.observability.token_monitor.get_redis", return_value=None):
            result = update_context_metrics({})
        assert isinstance(result, str)
        assert len(result) > 0

    def test_should_warn_no_session_id(self):
        """Empty session_id with Redis unavailable → still warns."""
        from hooks.observability.token_monitor import should_warn_context

        with patch("hooks.observability.token_monitor.get_redis", return_value=None):
            warn, level = should_warn_context(65.0, "")
        assert warn is True
        assert level == "warning"


@pytest.mark.feature("tokens-gauges-hive-daemon")
def test_native_statusline_writes_its_fill_into_the_seen_record_without_otel(monkeypatch, capsys, tmp_path):
    import io
    import json

    from hooks import config, statusline
    from hooks.context import context_usage

    monkeypatch.setattr(context_usage, "AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr(config, "TOKEN_MONITOR_ENABLED", True)
    payload = {
        "session_id": "native-claude",
        "context_window": {"context_window_size": 200000, "used_percentage": 25},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    with (
        patch("hooks.observability.token_monitor.get_redis", return_value=None),
        patch("hooks.observability.otel.record_gauge") as gauge,
        patch("hooks.observability.otel.flush") as flush,
    ):
        statusline.main()
    gauge.assert_not_called()
    flush.assert_not_called()
    assert _tokens("native-claude") == {"session_id": "native-claude", "fill_pct": 25.0, "burn_rate": None, "at_s": ANY}
    assert "25%" in capsys.readouterr().out


@pytest.mark.parametrize("target", ["claude", "codex"])
@pytest.mark.parametrize("enabled,window", [(False, 200000), (True, 0), (True, None)])
def test_native_context_without_measurement_emits_no_gauge(monkeypatch, tmp_path, target, enabled, window):
    import io
    import json

    from hooks import config, statusline
    from hooks.context import context_usage
    from hooks.observability.token_monitor import record_lifecycle_context
    from scripts.codex_context import CodexContext

    monkeypatch.setattr(context_usage, "AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr(config, "TOKEN_MONITOR_ENABLED", enabled)
    monkeypatch.setenv("AGENTIHOOKS_TARGET", target)
    payload = {"session_id": "empty", "hook_event_name": "PostToolUse"}
    if window is not None:
        payload["context_window"] = {"context_window_size": window, "used_percentage": 25}
    context = CodexContext(50000, window) if window is not None else None
    with (
        patch("scripts.codex_context.codex_context", return_value=context),
        patch("hooks.observability.otel.record_gauge") as gauge,
        patch("hooks.observability.otel.flush"),
    ):
        if target == "claude":
            monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
            statusline.main()
        else:
            record_lifecycle_context(payload)
    gauge.assert_not_called()
    assert _tokens("empty") == {}


@pytest.mark.feature("tokens-gauges-hive-daemon")
@pytest.mark.parametrize("event", ["PostToolUse", "Stop"])
def test_codex_context_growth_and_compaction(monkeypatch, event):
    from hooks import config
    from hooks.observability.token_monitor import record_lifecycle_context
    from scripts.codex_context import CodexContext

    monkeypatch.setattr(config, "TOKEN_MONITOR_ENABLED", True)
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    previous = 40000
    with (
        patch(
            "scripts.codex_context.codex_context",
            side_effect=[CodexContext(50000, 200000), CodexContext(10000, 200000)],
        ),
        patch("hooks.observability.token_monitor._get_previous_used", side_effect=[previous, 50000]),
        patch("hooks.observability.token_monitor.persist_token_metrics") as persist,
        patch("hooks.observability.otel.record_gauge") as gauge,
    ):
        payload = {"session_id": "growth", "hook_event_name": event}
        record_lifecycle_context(payload)
        record_lifecycle_context(payload)
    gauge.assert_not_called()
    assert _tokens("growth") == {"session_id": "growth", "fill_pct": 5.0, "burn_rate": 0.0, "at_s": ANY}
    assert persist.call_args.args[1]["used"] == 10000


@pytest.mark.feature("tokens-gauges-hive-daemon")
@pytest.mark.parametrize("window,used", [(200000, 50000), (1, 0)])
def test_lifecycle_reads_the_current_session_rollout(monkeypatch, tmp_path, window, used):
    import json

    from hooks import config
    from hooks.observability.token_monitor import record_lifecycle_context

    monkeypatch.setattr(config, "TOKEN_MONITOR_ENABLED", True)
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    day = tmp_path / "sessions" / "2026" / "10" / "07"
    day.mkdir(parents=True)
    rollout = day / "rollout-current-session.jsonl"
    rollout.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"last_token_usage": {"total_tokens": used}, "model_context_window": window},
                },
            }
        )
        + "\n"
    )
    with (
        patch("hooks.observability.token_monitor.get_redis", return_value=None),
        patch("hooks.observability.otel.record_gauge") as gauge,
    ):
        record_lifecycle_context({"session_id": "current-session", "hook_event_name": "PostToolUse"})
    gauge.assert_not_called()
    assert _tokens("current-session")["fill_pct"] == (25.0 if used else 0.0)


@pytest.mark.feature("tokens-gauges-hive-daemon")
def test_a_status_line_run_never_starts_the_otel_worker_or_sdk():
    payload = {"session_id": "status-seam", "context_window": {"context_window_size": 200000, "used_percentage": 25}}
    run = f"import io, sys\nsys.stdin = io.StringIO({json.dumps(payload)!r})\nfrom hooks import statusline\nstatusline.main()"
    assert _otel_after(run, {}) == {"initialized": False, "worker": False}
    assert _tokens("status-seam")["fill_pct"] == 25.0


@pytest.mark.feature("tokens-gauges-hive-daemon")
def test_a_codex_lifecycle_record_never_starts_the_otel_worker_or_sdk(tmp_path):
    day = tmp_path / "sessions" / "2026" / "10" / "07"
    day.mkdir(parents=True)
    usage = {"last_token_usage": {"total_tokens": 50000}, "model_context_window": 200000}
    line = {"type": "event_msg", "payload": {"type": "token_count", "info": usage}}
    (day / "rollout-codex-seam.jsonl").write_text(json.dumps(line) + "\n")
    run = (
        "from hooks.observability.token_monitor import record_lifecycle_context\n"
        "record_lifecycle_context({'session_id': 'codex-seam', 'hook_event_name': 'PostToolUse'})"
    )
    env = {"AGENTIHOOKS_TARGET": "codex", "CODEX_HOME": str(tmp_path)}
    assert _otel_after(run, env) == {"initialized": False, "worker": False}
    assert _tokens("codex-seam")["fill_pct"] == 25.0


@pytest.mark.feature("tokens-gauges-hive-daemon")
def test_the_presence_and_token_writers_keep_each_others_values_in_the_seen_record(monkeypatch):
    from hooks.observability import otel, presence
    from hooks.observability.token_monitor import record_tokens

    monkeypatch.setattr(otel, "record_gauge", lambda *args: None)
    path = presence.seen_dir() / "sess-merge.json"
    record_tokens("sess-merge", 40.0, 500.0, 1_000.0)
    assert json.loads(path.read_text())["seen_s"] == 1_000.0
    presence.agent("sess-merge", {"AGENTIHOOKS_HIVE_ID": "hive-1"}, 1_001.0)
    assert _tokens("sess-merge") == {"session_id": "sess-merge", "fill_pct": 40.0, "burn_rate": 500.0, "at_s": 1_000.0}
    record_tokens("sess-merge", 41.0, None, 1_002.0)
    record = json.loads(path.read_text())
    assert record["tokens"] == {"session_id": "sess-merge", "fill_pct": 41.0, "burn_rate": None, "at_s": 1_002.0}
    assert record["seen_s"] == 1_001.0
    assert record["attributes"]["session.id"] == "sess-merge"
    assert record["sent_s"] == 1_001.0
