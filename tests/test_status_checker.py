"""Tests for scripts.status_checker."""

import json
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


class TestCheckProfile:
    @staticmethod
    def _check(tmp_path, state):
        from scripts.status_checker import check_profile

        with patch("scripts.status_checker.STATE_JSON", tmp_path / "state.json"):
            (tmp_path / "state.json").write_text(json.dumps(state))
            return check_profile()

    def test_with_state(self, tmp_path):
        """targets.global is keyed by install target — the flat read reports nothing."""
        result = self._check(
            tmp_path,
            {"targets": {"global": {"claude": {"profile": "anton"}}}, "bundle": {"path": str(tmp_path)}},
        )
        assert result["name"] == "anton"
        assert result["ok"] is True
        assert [t["target"] for t in result["targets"]] == ["claude"]

    def test_reports_every_installed_target(self, tmp_path):
        result = self._check(
            tmp_path,
            {
                "targets": {
                    "global": {
                        "claude": {"profile": "anton,brain"},
                        "codex": {"profile": "anton", "settings_profile": "lean"},
                    }
                },
                "install_target": "codex",
            },
        )
        assert [t["target"] for t in result["targets"]] == ["claude", "codex"]
        # Headline follows install_target, so `status` speaks for the target
        # the operator last installed rather than always for claude.
        assert result["name"] == "anton"
        assert result["settings_profile"] == "lean"

    def test_linked_profile_names_the_targets_carrying_it(self, tmp_path):
        result = self._check(
            tmp_path,
            {
                "targets": {"global": {"claude": {"profile": "anton,brain"}, "codex": {"profile": "anton"}}},
                "linked_profiles": [{"name": "brain", "path": str(tmp_path)}],
            },
        )
        lp = result["linked_profiles"][0]
        assert lp["in_chain"] is True
        assert lp["in_targets"] == ["claude"]  # divergence is visible, not collapsed

    def test_legacy_flat_state_still_reads(self, tmp_path):
        """Pre-multi-target state files must not regress to '(not installed)'."""
        result = self._check(tmp_path, {"targets": {"global": {"profile": "anton"}}})
        assert result["name"] == "anton"
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

        # config values are module constants read at import — patch the config
        # attributes directly (patching os.environ would not reload them).
        # context_compression is active only when scope == "all" (the default
        # "refresh" is a no-op since the context-refresh path was removed).
        with (
            patch("hooks.config.BASH_FILTER_ENABLED", True),
            patch("hooks.config.FILE_READ_CACHE_ENABLED", True),
            patch("hooks.config.CONTEXT_REFRESH_COMPRESSION", "standard"),
            patch("hooks.config.CONTEXT_COMPRESSION_SCOPE", "all"),
            patch("hooks.config.CONTEXT_AUDIT_ENABLED", True),
            patch("hooks.config.EFFORT_POLICY_ENABLED", True),
            patch("hooks.config.COMPACT_SUGGEST_ENABLED", True),
        ):
            result = check_guardrails()
            assert result["active"] == 6
            assert result["total"] == 6

    def test_compression_inactive_when_scope_default(self):
        """scope=refresh is a no-op post context-refresh removal — must not report active."""
        from scripts.status_checker import check_guardrails

        with (
            patch("hooks.config.CONTEXT_REFRESH_COMPRESSION", "standard"),
            patch("hooks.config.CONTEXT_COMPRESSION_SCOPE", "refresh"),
        ):
            result = check_guardrails()
            assert result["details"]["context_compression"] is False


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
            patch("scripts.status_checker._query_mcp_tools", return_value=None),
        ):
            (tmp_path / ".claude").mkdir()
            result = check_mcp()
            assert result["total"] == 2
            assert result["enabled"] == 2
            assert "test-server" in result["servers"]
            assert result["servers"]["test-server"]["type"] == "stdio"
            assert result["servers"]["test-http"]["type"] == "http"
            assert result["ok"] is True

    @pytest.mark.parametrize(
        ("cached_at", "expected"),
        [
            ("2026-01-01T12:00:00+00:00", {"fixture": 8}),
            ("2026-01-01T11:00:00+00:00", {}),
        ],
    )
    def test_cache_lifetime(self, tmp_path, monkeypatch, cached_at, expected):
        from datetime import datetime, timezone

        from scripts import status_checker

        cache = tmp_path / "counts.json"
        cache.write_text(json.dumps({"_cached_at": cached_at, "servers": {"fixture": 8}}))
        monkeypatch.setattr(status_checker, "_MCP_CACHE_FILE", cache)
        monkeypatch.setattr(status_checker, "_MCP_CACHE_TTL", 600)
        now = datetime(2026, 1, 1, 12, 5, tzinfo=timezone.utc)
        parse = datetime.fromisoformat
        with patch("datetime.datetime") as clock:
            clock.now.return_value = now
            clock.fromisoformat.side_effect = parse
            assert status_checker._load_tool_cache() == expected


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
            "quota": {"summary": "(not configured)", "ok": False},
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


# ---------------------------------------------------------------------------
# P0.4 — agentihooks doctor --debug-hook
# ---------------------------------------------------------------------------


class TestHookInjectionProbe:
    def test_synthetic_payload_minimal_fields(self):
        from scripts.status_checker import _synthetic_payload

        for ev in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"):
            p = _synthetic_payload(ev)
            assert p["session_id"] == "doctor-probe"
            assert p["hook_event_name"] == ev

    def test_synthetic_payload_pretool_includes_tool_fields(self):
        from scripts.status_checker import _synthetic_payload

        p = _synthetic_payload("PreToolUse")
        assert p["tool_name"] == "Bash"
        assert p["tool_input"] == {"command": "true"}

    def test_format_hook_injection_renders_passing_run(self):
        from scripts.status_checker import format_hook_injection

        result = {
            "ok": True,
            "events": [
                {
                    "event": "SessionStart",
                    "ok": True,
                    "exit_code": 0,
                    "stdout_bytes": 0,
                    "additional_context_chars": 0,
                    "reason": None,
                    "stderr_first_line": "",
                }
            ],
            "warnings": [],
        }
        out = format_hook_injection(result)
        assert "Overall: OK" in out
        assert "✓" in out

    def test_format_hook_injection_renders_failure(self):
        from scripts.status_checker import format_hook_injection

        result = {
            "ok": False,
            "events": [
                {
                    "event": "PreToolUse",
                    "ok": False,
                    "exit_code": 1,
                    "stdout_bytes": 12,
                    "additional_context_chars": 0,
                    "reason": "exit code 1 (expected 0 or 2)",
                    "stderr_first_line": "boom",
                }
            ],
            "warnings": ["PreToolUse: exit code 1 (expected 0 or 2)"],
        }
        out = format_hook_injection(result)
        assert "Overall: FAILED" in out
        assert "exit code 1" in out
        assert "stderr: boom" in out

    def test_check_hook_injection_real_run(self):
        """Smoke test against the real hook process. Asserts the probe runs
        end-to-end without raising; does NOT assert ok=True (fleet may have
        legitimate findings that the operator routes separately)."""
        from scripts.status_checker import check_hook_injection

        result = check_hook_injection()
        assert "ok" in result
        assert "events" in result
        assert len(result["events"]) == 5
        for ev in result["events"]:
            assert ev["event"] in {
                "SessionStart",
                "UserPromptSubmit",
                "PreToolUse",
                "PostToolUse",
                "Stop",
            }

    def test_check_hook_injection_starts_the_five_events_side_by_side(self):
        import subprocess
        import threading

        from scripts.status_checker import check_hook_injection

        barrier = threading.Barrier(5, timeout=5)

        def fake_run(cmd, **kwargs):
            barrier.wait()
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=fake_run):
            result = check_hook_injection()

        assert [ev["event"] for ev in result["events"]] == [
            "SessionStart",
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "Stop",
        ]
        assert result["ok"] is True


class TestFormatterTargetLines:
    """The per-target lines and the `targets` JSON key are the whole point of
    the multi-target status fix; TestFormatters' fixtures predate them."""

    RESULT = {
        "name": "anton,brain",
        "settings_profile": "",
        "targets": [
            {"target": "claude", "profile": "anton,brain", "settings_profile": "", "path": "/h/.claude"},
            {"target": "codex", "profile": "anton", "settings_profile": "lean", "path": "/h/.codex"},
        ],
        "bundle": "(none)",
        "bundle_ok": True,
        "linked_profiles": [
            {"name": "brain", "path": "/p/brain", "in_chain": True, "in_targets": ["claude"], "exists": True}
        ],
        "ok": True,
    }

    def _full(self):
        return {
            "profile": self.RESULT,
            "hooks": {"total": 10, "expected": 10, "ok": True},
            "python": {"path": "/x/python", "ok": True},
            "redis": {"connected": False, "session_count": 0, "ok": False},
            "otel": {"enabled": False, "ok": True},
            "guardrails": {"active": 0, "total": 0, "details": {}, "ok": True},
            "mcp": {"total": 0, "enabled": 0, "disabled": 0, "servers": {}, "ok": True},
            "quota": {"summary": "(not configured)", "ok": False},
        }

    def test_cli_prints_one_line_per_target(self):
        from scripts.status_checker import format_cli

        out = format_cli(self._full())
        assert "+ target claude: anton,brain" in out
        assert "+ target codex: anton | settings: lean" in out

    def test_cli_names_the_targets_carrying_a_linked_profile(self):
        from scripts.status_checker import format_cli

        assert "(in chain: claude)" in format_cli(self._full())

    def test_cli_survives_a_result_with_no_targets_key(self):
        """Older callers / hand-built fixtures must not crash the formatter."""
        from scripts.status_checker import format_cli

        stripped = self._full()
        stripped["profile"] = {k: v for k, v in self.RESULT.items() if k != "targets"}
        assert "Profile: anton,brain" in format_cli(stripped)

    def test_json_carries_the_targets_array(self, tmp_path):
        """Built from a real check_profile(), not a literal — format_json is a
        bare json.dumps, so a hand-written fixture would assert only itself."""
        import json as _json

        from scripts.status_checker import check_profile, format_json

        state = {
            "targets": {"global": {"claude": {"profile": "anton"}, "codex": {"profile": "smith"}}},
            "install_target": "codex",
        }
        with patch("scripts.status_checker.STATE_JSON", tmp_path / "state.json"):
            (tmp_path / "state.json").write_text(json.dumps(state))
            results = dict(self._full(), profile=check_profile())
        parsed = _json.loads(format_json(results))
        assert [t["target"] for t in parsed["profile"]["targets"]] == ["claude", "codex"]
        assert parsed["profile"]["name"] == "smith"


class TestCheckProfileHeadlineFallback:
    """check_profile resolves its headline record install_target -> claude ->
    first-present. Only the first two arms had coverage."""

    @staticmethod
    def _check(tmp_path, state):
        from scripts.status_checker import check_profile

        with patch("scripts.status_checker.STATE_JSON", tmp_path / "state.json"):
            (tmp_path / "state.json").write_text(json.dumps(state))
            return check_profile()

    def test_falls_back_to_the_only_target_when_install_target_absent(self, tmp_path):
        result = self._check(tmp_path, {"targets": {"global": {"codex": {"profile": "smith"}}}})
        assert result["name"] == "smith"
        assert result["ok"] is True

    def test_falls_back_to_claude_when_install_target_names_an_absent_target(self, tmp_path):
        result = self._check(
            tmp_path,
            {"targets": {"global": {"claude": {"profile": "anton"}}}, "install_target": "codex"},
        )
        assert result["name"] == "anton"
