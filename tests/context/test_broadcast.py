"""Tests for hooks.context.broadcast."""

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def broadcast_dir(tmp_path):
    """Provide a temp dir for broadcast and session files."""
    return tmp_path


@pytest.fixture()
def broadcast_file(broadcast_dir):
    return broadcast_dir / "broadcast.json"


@pytest.fixture()
def sessions_file(broadcast_dir):
    return broadcast_dir / "active-sessions.json"


# ---------------------------------------------------------------------------
# Broadcast file I/O
# ---------------------------------------------------------------------------


class TestBroadcastFileIO:
    def test_load_empty_file(self, broadcast_file):
        from hooks.context.broadcast import _load_broadcasts

        with patch("hooks.context.broadcast._broadcast_path", return_value=broadcast_file):
            msgs = _load_broadcasts()
        assert msgs == []

    def test_load_nonexistent_file(self, broadcast_file):
        from hooks.context.broadcast import _load_broadcasts

        with patch("hooks.context.broadcast._broadcast_path", return_value=broadcast_file):
            msgs = _load_broadcasts()
        assert msgs == []

    def test_save_and_load_roundtrip(self, broadcast_file):
        from hooks.context.broadcast import _load_broadcasts, _save_broadcasts

        messages = [
            {
                "id": "test-1",
                "message": "Deploy freeze",
                "severity": "alert",
                "persistent": True,
                "source": "operator",
                "created_at": "2026-04-07T22:00:00Z",
                "ttl_seconds": 3600,
                "expires_at": "2026-04-08T03:00:00Z",
                "delivered_to": [],
            }
        ]
        with patch("hooks.context.broadcast._broadcast_path", return_value=broadcast_file):
            _save_broadcasts(messages)
            loaded = _load_broadcasts()
        assert len(loaded) == 1
        assert loaded[0]["id"] == "test-1"
        assert loaded[0]["message"] == "Deploy freeze"

    def test_atomic_write(self, broadcast_file):
        """Verify writes go through .tmp then os.replace."""
        from hooks.context.broadcast import _save_broadcasts

        with patch("hooks.context.broadcast._broadcast_path", return_value=broadcast_file):
            _save_broadcasts([{"id": "test", "message": "msg"}])
        assert broadcast_file.exists()
        # No .tmp file should remain
        assert not broadcast_file.with_suffix(".tmp").exists()


# ---------------------------------------------------------------------------
# Message creation
# ---------------------------------------------------------------------------


class TestCreateBroadcast:
    def test_create_alert(self, broadcast_file):
        from hooks.context.broadcast import create_broadcast, _load_broadcasts

        with patch("hooks.context.broadcast._broadcast_path", return_value=broadcast_file):
            msg_id = create_broadcast("Deploy freeze", severity="alert")
            msgs = _load_broadcasts()

        assert msg_id is not None
        assert len(msgs) == 1
        assert msgs[0]["severity"] == "alert"
        assert msgs[0]["persistent"] is True  # alert default

    def test_create_critical(self, broadcast_file):
        from hooks.context.broadcast import create_broadcast, _load_broadcasts

        with patch("hooks.context.broadcast._broadcast_path", return_value=broadcast_file):
            create_broadcast("Incident!", severity="critical", ttl_seconds=1800)
            msgs = _load_broadcasts()

        assert msgs[0]["severity"] == "critical"
        assert msgs[0]["ttl_seconds"] == 1800
        assert msgs[0]["persistent"] is True

    def test_create_info_not_persistent(self, broadcast_file):
        from hooks.context.broadcast import create_broadcast, _load_broadcasts

        with patch("hooks.context.broadcast._broadcast_path", return_value=broadcast_file):
            create_broadcast("SonarQube down", severity="info")
            msgs = _load_broadcasts()

        assert msgs[0]["persistent"] is False

    def test_create_with_custom_ttl(self, broadcast_file):
        from hooks.context.broadcast import create_broadcast, _load_broadcasts

        with patch("hooks.context.broadcast._broadcast_path", return_value=broadcast_file):
            create_broadcast("Custom TTL", severity="alert", ttl_seconds=7200)
            msgs = _load_broadcasts()

        assert msgs[0]["ttl_seconds"] == 7200

    def test_max_messages_enforced(self, broadcast_file):
        from hooks.context.broadcast import create_broadcast, _load_broadcasts

        with (
            patch("hooks.context.broadcast._broadcast_path", return_value=broadcast_file),
            patch("hooks.context.broadcast.BROADCAST_MAX_MESSAGES", 3),
        ):
            for i in range(5):
                create_broadcast(f"Message {i}", severity="info")
            msgs = _load_broadcasts()

        assert len(msgs) == 3
        # Should keep the newest
        assert msgs[-1]["message"] == "Message 4"


# ---------------------------------------------------------------------------
# Expiry and cleanup
# ---------------------------------------------------------------------------


class TestExpiry:
    def test_expired_messages_cleaned_on_load(self, broadcast_file):
        from hooks.context.broadcast import _load_broadcasts, _save_broadcasts

        expired_msg = {
            "id": "expired-1",
            "message": "Old alert",
            "severity": "alert",
            "persistent": True,
            "source": "operator",
            "created_at": "2026-04-06T00:00:00Z",
            "ttl_seconds": 60,
            "expires_at": "2026-04-06T00:01:00Z",  # already expired
            "delivered_to": [],
        }
        with patch("hooks.context.broadcast._broadcast_path", return_value=broadcast_file):
            _save_broadcasts([expired_msg])
            msgs = _load_broadcasts(cleanup=True)

        assert len(msgs) == 0


# ---------------------------------------------------------------------------
# Delivery tracking
# ---------------------------------------------------------------------------


class TestDelivery:
    def test_one_shot_marks_delivered(self, broadcast_file):
        from hooks.context.broadcast import create_broadcast, get_pending_broadcasts, mark_delivered

        with patch("hooks.context.broadcast._broadcast_path", return_value=broadcast_file):
            create_broadcast("Info msg", severity="info")
            pending = get_pending_broadcasts("sess-1")
            assert len(pending) == 1

            mark_delivered("sess-1", pending[0]["id"])
            pending_after = get_pending_broadcasts("sess-1")
            assert len(pending_after) == 0

    def test_persistent_always_pending(self, broadcast_file):
        from hooks.context.broadcast import create_broadcast, get_pending_broadcasts, mark_delivered

        with patch("hooks.context.broadcast._broadcast_path", return_value=broadcast_file):
            create_broadcast("Alert msg", severity="alert")  # persistent by default
            pending = get_pending_broadcasts("sess-1")
            assert len(pending) == 1

            mark_delivered("sess-1", pending[0]["id"])
            # Persistent messages are still pending (re-injected every turn)
            pending_after = get_pending_broadcasts("sess-1")
            assert len(pending_after) == 1

    def test_different_sessions_independent(self, broadcast_file):
        from hooks.context.broadcast import create_broadcast, get_pending_broadcasts, mark_delivered

        with patch("hooks.context.broadcast._broadcast_path", return_value=broadcast_file):
            create_broadcast("Info msg", severity="info")

            mark_delivered("sess-1", get_pending_broadcasts("sess-1")[0]["id"])

            # sess-2 should still see it
            pending_sess2 = get_pending_broadcasts("sess-2")
            assert len(pending_sess2) == 1


# ---------------------------------------------------------------------------
# Severity filtering for PreToolUse
# ---------------------------------------------------------------------------


class TestCriticalFiltering:
    def test_get_critical_only_returns_critical(self, broadcast_file):
        from hooks.context.broadcast import create_broadcast, get_critical_broadcasts

        with patch("hooks.context.broadcast._broadcast_path", return_value=broadcast_file):
            create_broadcast("Alert", severity="alert")
            create_broadcast("Critical!", severity="critical")
            create_broadcast("Info", severity="info")

            critical = get_critical_broadcasts("sess-1")

        assert len(critical) == 1
        assert critical[0]["severity"] == "critical"

    def test_critical_returns_empty_when_none(self, broadcast_file):
        from hooks.context.broadcast import create_broadcast, get_critical_broadcasts

        with patch("hooks.context.broadcast._broadcast_path", return_value=broadcast_file):
            create_broadcast("Just info", severity="info")
            critical = get_critical_broadcasts("sess-1")

        assert len(critical) == 0


# ---------------------------------------------------------------------------
# Session registry
# ---------------------------------------------------------------------------


class TestSessionRegistry:
    def test_register_session(self, broadcast_dir):
        from hooks.context.broadcast import register_session, get_active_sessions

        sessions_file = broadcast_dir / "active-sessions.json"
        with patch("hooks.context.broadcast._sessions_path", return_value=sessions_file):
            register_session("sess-1", pid=os.getpid(), cwd="/home/user/project", model="opus")
            sessions = get_active_sessions()

        assert "sess-1" in sessions
        assert sessions["sess-1"]["cwd"] == "/home/user/project"

    def test_deregister_session(self, broadcast_dir):
        from hooks.context.broadcast import register_session, deregister_session, get_active_sessions

        sessions_file = broadcast_dir / "active-sessions.json"
        with patch("hooks.context.broadcast._sessions_path", return_value=sessions_file):
            register_session("sess-1", pid=os.getpid(), cwd="/tmp", model="sonnet")
            deregister_session("sess-1")
            sessions = get_active_sessions()

        assert "sess-1" not in sessions

    def test_stale_session_cleaned(self, broadcast_dir):
        from hooks.context.broadcast import get_active_sessions, _save_sessions

        sessions_file = broadcast_dir / "active-sessions.json"
        stale = {
            "sess-dead": {
                "started_at": "2026-04-07T00:00:00Z",
                "pid": 999999999,  # non-existent PID
                "cwd": "/tmp",
                "model": "opus",
            }
        }
        with patch("hooks.context.broadcast._sessions_path", return_value=sessions_file):
            _save_sessions(stale)
            sessions = get_active_sessions(cleanup=True)

        assert "sess-dead" not in sessions


# ---------------------------------------------------------------------------
# Banner formatting
# ---------------------------------------------------------------------------


class TestBannerFormatting:
    def test_format_broadcast_banner(self):
        from hooks.context.broadcast import format_broadcast_banner

        msg = {
            "message": "Deploy freeze until 3am",
            "severity": "alert",
            "source": "operator",
            "expires_at": "2026-04-08T03:00:00Z",
        }
        banner = format_broadcast_banner(msg)
        assert "Deploy freeze until 3am" in banner
        assert "ALERT" in banner.upper()
        assert "operator" in banner

    def test_format_critical_context(self):
        from hooks.context.broadcast import format_critical_context

        msgs = [
            {"message": "Incident — read only", "severity": "critical", "expires_at": "2026-04-08T00:30:00Z"},
            {"message": "Cred rotation", "severity": "critical", "expires_at": "2026-04-08T01:00:00Z"},
        ]
        context = format_critical_context(msgs)
        assert "Incident" in context
        assert "Cred rotation" in context
        assert "CRITICAL" in context.upper()


# ---------------------------------------------------------------------------
# Clear and list
# ---------------------------------------------------------------------------


class TestClearAndList:
    def test_clear_all(self, broadcast_file):
        from hooks.context.broadcast import create_broadcast, clear_broadcasts, _load_broadcasts

        with patch("hooks.context.broadcast._broadcast_path", return_value=broadcast_file):
            create_broadcast("msg1", severity="info")
            create_broadcast("msg2", severity="alert")
            clear_broadcasts()
            msgs = _load_broadcasts()

        assert len(msgs) == 0

    def test_clear_specific(self, broadcast_file):
        from hooks.context.broadcast import create_broadcast, clear_broadcasts, _load_broadcasts

        with patch("hooks.context.broadcast._broadcast_path", return_value=broadcast_file):
            id1 = create_broadcast("msg1", severity="info")
            id2 = create_broadcast("msg2", severity="alert")
            clear_broadcasts(message_id=id1)
            msgs = _load_broadcasts()

        assert len(msgs) == 1
        assert msgs[0]["id"] == id2

    def test_list_broadcasts(self, broadcast_file):
        from hooks.context.broadcast import create_broadcast, list_broadcasts

        with patch("hooks.context.broadcast._broadcast_path", return_value=broadcast_file):
            create_broadcast("msg1", severity="info")
            create_broadcast("msg2", severity="critical")
            result = list_broadcasts()

        assert len(result) == 2


# ---------------------------------------------------------------------------
# Integration: hook injection
# ---------------------------------------------------------------------------


class TestHookIntegration:
    def test_check_broadcasts_injects_banner(self, broadcast_file, capsys):
        from hooks.context.broadcast import create_broadcast, check_and_inject_broadcasts

        with (
            patch("hooks.context.broadcast._broadcast_path", return_value=broadcast_file),
            patch("hooks.context.broadcast.BROADCAST_ENABLED", True),
        ):
            create_broadcast("Test alert", severity="alert")
            check_and_inject_broadcasts("sess-test")

        captured = capsys.readouterr().out
        assert "Test alert" in captured
        assert "BROADCAST" in captured

    def test_check_broadcasts_disabled(self, broadcast_file, capsys):
        from hooks.context.broadcast import create_broadcast, check_and_inject_broadcasts

        with (
            patch("hooks.context.broadcast._broadcast_path", return_value=broadcast_file),
            patch("hooks.context.broadcast.BROADCAST_ENABLED", False),
        ):
            create_broadcast("Should not appear", severity="alert")
            check_and_inject_broadcasts("sess-test")

        captured = capsys.readouterr().out
        assert "Should not appear" not in captured

    def test_get_pretool_context_returns_critical(self, broadcast_file):
        from hooks.context.broadcast import create_broadcast, get_pretool_context

        with (
            patch("hooks.context.broadcast._broadcast_path", return_value=broadcast_file),
            patch("hooks.context.broadcast.BROADCAST_ENABLED", True),
            patch("hooks.context.broadcast.BROADCAST_CRITICAL_ON_PRETOOL", True),
        ):
            create_broadcast("Critical incident", severity="critical")
            context = get_pretool_context("sess-test")

        assert context is not None
        assert "Critical incident" in context

    def test_get_pretool_context_ignores_non_critical(self, broadcast_file):
        from hooks.context.broadcast import create_broadcast, get_pretool_context

        with (
            patch("hooks.context.broadcast._broadcast_path", return_value=broadcast_file),
            patch("hooks.context.broadcast.BROADCAST_ENABLED", True),
            patch("hooks.context.broadcast.BROADCAST_CRITICAL_ON_PRETOOL", True),
        ):
            create_broadcast("Just an alert", severity="alert")
            context = get_pretool_context("sess-test")

        assert context is None

    def test_get_pretool_context_disabled(self, broadcast_file):
        from hooks.context.broadcast import create_broadcast, get_pretool_context

        with (
            patch("hooks.context.broadcast._broadcast_path", return_value=broadcast_file),
            patch("hooks.context.broadcast.BROADCAST_ENABLED", True),
            patch("hooks.context.broadcast.BROADCAST_CRITICAL_ON_PRETOOL", False),
        ):
            create_broadcast("Critical", severity="critical")
            context = get_pretool_context("sess-test")

        assert context is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_corrupt_broadcast_file(self, broadcast_file):
        from hooks.context.broadcast import _load_broadcasts

        broadcast_file.write_text("not json{{{")
        with patch("hooks.context.broadcast._broadcast_path", return_value=broadcast_file):
            msgs = _load_broadcasts()
        assert msgs == []

    def test_empty_message_rejected(self, broadcast_file):
        from hooks.context.broadcast import create_broadcast

        with patch("hooks.context.broadcast._broadcast_path", return_value=broadcast_file):
            result = create_broadcast("", severity="alert")
        assert result is None

    def test_invalid_severity_defaults_to_alert(self, broadcast_file):
        from hooks.context.broadcast import create_broadcast, _load_broadcasts

        with patch("hooks.context.broadcast._broadcast_path", return_value=broadcast_file):
            create_broadcast("msg", severity="nonexistent")
            msgs = _load_broadcasts()
        assert msgs[0]["severity"] == "alert"
