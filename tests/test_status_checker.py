"""Tests for scripts.status_checker."""

import json
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


class TestCheckProfile:
    def test_with_state(self, tmp_path):
        from scripts.status_checker import check_profile

        state = {"targets": {"global": {"profile": "colt"}}, "bundle": {"path": str(tmp_path)}}
        with patch("scripts.status_checker.STATE_JSON", tmp_path / "state.json"):
            (tmp_path / "state.json").write_text(json.dumps(state))
            result = check_profile()
            assert result["name"] == "colt"
            assert result["ok"] is True

    def test_missing_state(self, tmp_path):
        from scripts.status_checker import check_profile

        with patch("scripts.status_checker.STATE_JSON", tmp_path / "nonexist.json"):
            result = check_profile()
            assert result["ok"] is False


class TestCheckHooks:
    def test_all_hooks_present(self, tmp_path):
        from scripts.status_checker import check_hooks

        settings = {"hooks": {f"Event{i}": [] for i in range(10)}}
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps(settings))
        with patch("scripts.status_checker.CLAUDE_HOME", tmp_path):
            result = check_hooks()
            assert result["total"] == 10
            assert result["ok"] is True

    def test_missing_settings(self, tmp_path):
        from scripts.status_checker import check_hooks

        with patch("scripts.status_checker.CLAUDE_HOME", tmp_path):
            result = check_hooks()
            assert result["total"] == 0
            assert result["ok"] is False


class TestCheckPython:
    def test_venv_exists(self, tmp_path):

        from scripts.status_checker import check_python

        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python3").write_text("#!/usr/bin/env python3")

        # Create a fake settings.json with hook commands pointing to our python
        claude_home = tmp_path / ".claude"
        claude_home.mkdir()
        fake_python = str(venv_bin / "python3")
        (claude_home / "settings.json").write_text(
            json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"command": f"{fake_python} -m hooks"}]}]}})
        )

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = b"Python 3.12.0"
        mock_result.stderr = b""
        with (
            patch("scripts.status_checker.AGENTIHOOKS_HOME", tmp_path),
            patch("scripts.status_checker.CLAUDE_HOME", claude_home),
            patch("subprocess.run", return_value=mock_result),
        ):
            result = check_python()
            assert result["ok"] is True
            assert "python3" in result["path"]
            assert "3.12" in result.get("version", "")

    def test_no_python_in_settings(self, tmp_path):
        from scripts.status_checker import check_python

        # No settings.json → no Python path extractable
        with patch("scripts.status_checker.CLAUDE_HOME", tmp_path):
            result = check_python()
            assert result["ok"] is False


class TestCheckDaemons:
    def test_running_daemons(self, tmp_path):
        import os

        from scripts.status_checker import check_daemons

        pid = os.getpid()
        (tmp_path / "sync-daemon.pid").write_text(str(pid))
        (tmp_path / "quota-watcher.pid").write_text(str(pid))
        with patch("scripts.status_checker.AGENTIHOOKS_HOME", tmp_path):
            result = check_daemons()
            assert result["sync"]["alive"] is True
            assert result["quota"]["alive"] is True
            assert result["ok"] is True

    def test_no_daemons(self, tmp_path):
        from scripts.status_checker import check_daemons

        with patch("scripts.status_checker.AGENTIHOOKS_HOME", tmp_path):
            result = check_daemons()
            assert result["sync"]["alive"] is False
            assert result["quota"]["alive"] is False


class TestCheckRedis:
    def test_no_redis(self):
        from scripts.status_checker import check_redis

        with patch("hooks._redis.get_redis", return_value=None):
            result = check_redis()
            assert result["connected"] is False

    def test_redis_connected(self):
        from scripts.status_checker import check_redis

        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.scan.return_value = (0, [])
        with patch("hooks._redis.get_redis", return_value=mock_redis):
            result = check_redis()
            assert result["connected"] is True


class TestCheckGuardrails:
    def test_all_enabled(self):
        from scripts.status_checker import check_guardrails

        with patch.dict(
            "os.environ",
            {
                "BASH_FILTER_ENABLED": "true",
                "FILE_READ_CACHE_ENABLED": "true",
                "CONTEXT_AUDIT_ENABLED": "true",
                "EFFORT_POLICY_ENABLED": "true",
                "PEAK_HOURS_ENABLED": "true",
                "COMPACT_SUGGEST_ENABLED": "true",
            },
        ):
            result = check_guardrails()
            assert result["active"] == 6
            assert result["total"] == 6


class TestCheckMcp:
    def test_with_servers(self, tmp_path):
        from scripts.status_checker import check_mcp

        fake_json = tmp_path / ".claude.json"
        fake_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "test-server": {"command": "python3 -m test"},
                        "test-http": {"url": "http://localhost:8080/mcp"},
                    }
                }
            )
        )
        with (
            patch("scripts.status_checker.Path.home", return_value=tmp_path),
            patch("scripts.status_checker.CLAUDE_HOME", tmp_path / ".claude"),
        ):
            (tmp_path / ".claude").mkdir()
            result = check_mcp()
            assert result["total"] == 2
            assert result["enabled"] == 2
            assert "test-server" in result["servers"]
            assert result["servers"]["test-server"]["type"] == "stdio"
            assert result["servers"]["test-http"]["type"] == "http"
            assert result["ok"] is True


class TestCheckOtel:
    def test_disabled(self):
        from scripts.status_checker import check_otel

        result = check_otel()
        assert "enabled" in result
        assert result["ok"] is True


class TestFormatters:
    def test_format_cli_produces_string(self):
        from scripts.status_checker import format_cli, run_all_checks

        with patch("hooks._redis.get_redis", return_value=None):
            results = run_all_checks()
            output = format_cli(results)
            assert isinstance(output, str)
            assert "Profile" in output
            assert "Hooks" in output

    def test_format_json_valid(self):
        from scripts.status_checker import format_json, run_all_checks

        with patch("hooks._redis.get_redis", return_value=None):
            results = run_all_checks()
            output = format_json(results)
            parsed = json.loads(output)
            assert "profile" in parsed
            assert "hooks" in parsed
            assert "guardrails" in parsed

    def test_format_cli_with_session(self):
        from scripts.status_checker import format_cli

        results = {
            "profile": {"name": "test", "bundle": "(none)", "ok": True},
            "hooks": {"total": 10, "expected": 10, "ok": True},
            "python": {"path": "/usr/bin/python3", "ok": True},
            "daemons": {"sync": {"pid": None, "alive": False}, "quota": {"pid": None, "alive": False}, "ok": False},
            "redis": {"connected": False, "session_count": 0, "ok": False},
            "otel": {"enabled": False, "ok": True},
            "guardrails": {
                "active": 6,
                "total": 6,
                "details": {
                    "bash_filter": True,
                    "file_dedup": True,
                    "context_audit": True,
                    "effort_policy": True,
                    "peak_hours": True,
                    "compact_suggest": True,
                },
                "ok": True,
            },
            "mcp": {
                "total": 2,
                "enabled": 2,
                "disabled": 0,
                "servers": {
                    "s1": {"type": "stdio", "source": "user", "enabled": True},
                    "s2": {"type": "http", "source": "user", "enabled": True},
                },
                "ok": True,
            },
            "quota": {"summary": "(not configured)", "peak": "off-peak", "ok": False},
            "session": {
                "id": "test-123",
                "fill_pct": 42.5,
                "burn_rate": 1200,
                "used": 50000,
                "remaining": 67000,
                "tool_audit": {"Bash": 30000, "Read": 20000},
                "ok": True,
            },
        }
        output = format_cli(results)
        assert "Session metrics" in output
        assert "42%" in output
        assert "Bash" in output


class TestRunAllChecks:
    def test_without_session(self):
        from scripts.status_checker import run_all_checks

        with patch("hooks._redis.get_redis", return_value=None):
            results = run_all_checks()
            assert "session" not in results
            assert "profile" in results

    def test_with_session(self):
        from scripts.status_checker import run_all_checks

        with patch("hooks._redis.get_redis", return_value=None):
            results = run_all_checks(session_id="test-sess")
            assert "session" in results
