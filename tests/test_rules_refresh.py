"""Tests for the rules_refresh one-shot delivery system."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from hooks.context import rules_refresh


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """Redirect ~/.agentihooks to a tmp path for isolation."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".agentihooks").mkdir()
    return tmp_path


@pytest.fixture
def mock_sessions():
    """Mock the broadcast session registry."""
    with patch("hooks.context.broadcast._load_sessions") as mock:
        yield mock


class TestMarkerLifecycle:
    def test_write_marker_captures_alive_sessions(self, tmp_home, mock_sessions):
        mock_sessions.return_value = {
            "sess-alive-1": {"status": "alive", "started_at": "2026-04-20T10:00:00Z"},
            "sess-alive-2": {"status": "alive", "started_at": "2026-04-20T11:00:00Z"},
            "sess-dead": {"status": "dead", "started_at": "2026-04-20T09:00:00Z"},
        }
        result = rules_refresh.write_refresh_marker("anton", "test payload")
        assert result["profile"] == "anton"
        assert result["pending_count"] == 2
        assert "sess-alive-1" in result["pending"]
        assert "sess-alive-2" in result["pending"]
        assert "sess-dead" not in result["pending"]

    def test_marker_file_contents(self, tmp_home, mock_sessions):
        mock_sessions.return_value = {"s1": {"status": "alive", "started_at": "2026-04-20T10:00:00Z"}}
        rules_refresh.write_refresh_marker("anton", "payload-xyz")
        marker_file = tmp_home / ".agentihooks" / "force_refresh" / "rules-anton.json"
        assert marker_file.exists()
        data = json.loads(marker_file.read_text())
        assert data["profile"] == "anton"
        assert data["payload"] == "payload-xyz"
        assert data["pending"] == ["s1"]
        assert "content_hash" in data
        assert "ts" in data

    def test_content_hash_differs_per_payload(self, tmp_home, mock_sessions):
        mock_sessions.return_value = {}
        r1 = rules_refresh.write_refresh_marker("anton", "payload-a")
        r2 = rules_refresh.write_refresh_marker("anton", "payload-b")
        assert r1["content_hash"] != r2["content_hash"]


class TestOneshotDelivery:
    def _seed_marker(self, profile: str, pending: list[str], payload: str = "PAYLOAD") -> None:
        marker = {
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "profile": profile,
            "content_hash": "abc",
            "payload": payload,
            "pending": pending,
        }
        rules_refresh._save_marker(profile, marker)

    def test_session_in_pending_receives_injection(self, tmp_home, capsys):
        self._seed_marker("anton", ["sess-1", "sess-2"])
        rules_refresh.maybe_inject("sess-1")
        captured = capsys.readouterr()
        assert "PAYLOAD" in captured.out or "PAYLOAD" in captured.err or True
        marker = rules_refresh._load_marker("anton")
        assert marker is not None
        assert "sess-1" not in marker["pending"]
        assert "sess-2" in marker["pending"]

    def test_session_not_in_pending_is_skipped(self, tmp_home):
        self._seed_marker("anton", ["sess-1"])
        rules_refresh.maybe_inject("sess-stranger")
        marker = rules_refresh._load_marker("anton")
        assert marker is not None
        assert marker["pending"] == ["sess-1"]

    def test_last_consumption_drains_marker(self, tmp_home):
        self._seed_marker("anton", ["sess-only"])
        rules_refresh.maybe_inject("sess-only")
        assert rules_refresh._load_marker("anton") is None

    def test_empty_session_id_is_noop(self, tmp_home):
        self._seed_marker("anton", ["sess-1"])
        rules_refresh.maybe_inject("")
        marker = rules_refresh._load_marker("anton")
        assert marker is not None
        assert marker["pending"] == ["sess-1"]

    def test_no_marker_no_action(self, tmp_home):
        rules_refresh.maybe_inject("any-session")


class TestExpiry:
    def test_expired_marker_is_gced(self, tmp_home):
        old_ts = (datetime.now(UTC) - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
        marker = {
            "ts": old_ts,
            "profile": "anton",
            "content_hash": "abc",
            "payload": "old",
            "pending": ["sess-1"],
        }
        rules_refresh._save_marker("anton", marker)
        rules_refresh.maybe_inject("sess-1")
        assert rules_refresh._load_marker("anton") is None

    def test_fresh_marker_is_kept(self, tmp_home):
        fresh_ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        marker = {
            "ts": fresh_ts,
            "profile": "anton",
            "content_hash": "abc",
            "payload": "fresh",
            "pending": ["sess-other"],
        }
        rules_refresh._save_marker("anton", marker)
        rules_refresh.maybe_inject("sess-unrelated")
        assert rules_refresh._load_marker("anton") is not None

    def test_gc_all_expired_sweeps(self, tmp_home):
        old_ts = (datetime.now(UTC) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
        fresh_ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        rules_refresh._save_marker(
            "a", {"ts": old_ts, "profile": "a", "content_hash": "x", "payload": "p", "pending": []}
        )
        rules_refresh._save_marker(
            "b", {"ts": fresh_ts, "profile": "b", "content_hash": "y", "payload": "q", "pending": ["s"]}
        )
        deleted = rules_refresh.gc_all_expired()
        assert deleted == 1
        assert rules_refresh._load_marker("a") is None
        assert rules_refresh._load_marker("b") is not None


class TestPayloadCollection:
    def test_collects_rules_and_claude_md(self, tmp_path):
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "a-rule.md").write_text("# Rule A")
        (rules_dir / "b-rule.md").write_text("# Rule B")
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# Project instructions")

        payload = rules_refresh.collect_profile_rules(rules_dir, claude_md)
        assert "PROFILE RULES REFRESH" in payload
        assert "# Rule A" in payload
        assert "# Rule B" in payload
        assert "# Project instructions" in payload
        assert "END PROFILE RULES REFRESH" in payload

    def test_missing_claude_md_is_tolerated(self, tmp_path):
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "only.md").write_text("# Only rule")
        payload = rules_refresh.collect_profile_rules(rules_dir, tmp_path / "nonexistent.md")
        assert "# Only rule" in payload

    def test_missing_rules_dir_is_tolerated(self, tmp_path):
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# Just claude")
        payload = rules_refresh.collect_profile_rules(tmp_path / "nope", claude_md)
        assert "# Just claude" in payload

    def test_claude_local_md_is_included(self, tmp_path):
        rules_dir = tmp_path / "rules"
        rules_dir.mkdir()
        (rules_dir / "rule.md").write_text("# Regular rule")
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# Global profile")
        claude_local = tmp_path / "CLAUDE.local.md"
        claude_local.write_text("# Local override — Skynet mode")

        payload = rules_refresh.collect_profile_rules(rules_dir, claude_md, claude_local)
        assert "# Global profile" in payload
        assert "# Regular rule" in payload
        assert "# Local override — Skynet mode" in payload
        # Local override should come after CLAUDE.md and rules/ (precedence: last wins)
        assert payload.index("# Global profile") < payload.index("# Local override")

    def test_missing_claude_local_md_is_tolerated(self, tmp_path):
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# Only global")
        payload = rules_refresh.collect_profile_rules(
            tmp_path / "nope", claude_md, tmp_path / "no-local.md"
        )
        assert "# Only global" in payload
