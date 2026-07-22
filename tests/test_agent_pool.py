"""Tests for the agent pool — directed inbox, liveness routing, summaries, call_agent."""

import json
import os
import time
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_pool(tmp_path):
    """Redirect broadcast + pool state to tmp so nothing touches the real home."""
    bcast_file = tmp_path / "broadcast.json"
    sessions_file = tmp_path / "active-sessions.json"
    counter_file = tmp_path / "agent_pool_counters.json"
    with (
        patch("hooks.context.broadcast._broadcast_path", return_value=bcast_file),
        patch("hooks.context.broadcast._sessions_path", return_value=sessions_file),
        patch("hooks.context.agent_pool._counter_path", return_value=counter_file),
    ):
        yield


# ---------------------------------------------------------------------------
# Directed inbox delivery
# ---------------------------------------------------------------------------


class TestDirectedInbox:
    def test_matcher_delivers_only_to_target(self):
        from hooks.context.broadcast import _message_matches_channel

        msg = {"message": "hi", "target_session": "SID-A"}
        # Reaches the addressed session…
        assert _message_matches_channel(msg, [], "SID-A") is True
        # …and no one else, even a wildcard subscriber.
        assert _message_matches_channel(msg, ["*"], "SID-B") is False
        assert _message_matches_channel(msg, ["brain"], "SID-B") is False

    def test_directed_message_is_private_end_to_end(self):
        from hooks.context.broadcast import create_broadcast, get_pending_broadcasts

        mid = create_broadcast("back off, I'm on litellm", severity="alert", target_session="SID-A")
        assert mid

        pending_a = [m["id"] for m in get_pending_broadcasts("SID-A")]
        pending_b = [m["id"] for m in get_pending_broadcasts("SID-B")]
        assert mid in pending_a
        assert mid not in pending_b

    def test_non_directed_still_global(self):
        from hooks.context.broadcast import create_broadcast, get_pending_broadcasts

        mid = create_broadcast("everyone", severity="alert")
        assert mid in [m["id"] for m in get_pending_broadcasts("anyone")]


# ---------------------------------------------------------------------------
# Liveness routing
# ---------------------------------------------------------------------------


class TestLiveness:
    def test_alive_pid_is_live(self):
        from hooks.context.agent_pool import is_peer_live

        entry = {"pid": os.getpid(), "cwd": "/nope"}
        assert is_peer_live("SID", entry) is True

    def test_dead_pid_stale_transcript_is_dormant(self):
        from hooks.context.agent_pool import is_peer_live

        entry = {"pid": 999_999_999, "cwd": "/nope"}
        assert is_peer_live("SID", entry) is False

    def test_recent_transcript_is_live_even_without_pid(self, tmp_path):
        from hooks.context import agent_pool

        transcript = tmp_path / "live.jsonl"
        transcript.write_text("{}\n")
        with patch.object(agent_pool, "_transcript_path", return_value=transcript):
            assert agent_pool.is_peer_live("SID", {"pid": 0, "cwd": "/x"}) is True

    def test_old_transcript_is_dormant(self, tmp_path):
        from hooks.context import agent_pool

        transcript = tmp_path / "old.jsonl"
        transcript.write_text("{}\n")
        old = time.time() - 3600
        os.utime(transcript, (old, old))
        with patch.object(agent_pool, "_transcript_path", return_value=transcript):
            assert agent_pool.is_peer_live("SID", {"pid": 0, "cwd": "/x"}) is False


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------


class TestSummary:
    def _write_transcript(self, path):
        lines = [
            {"type": "user", "message": {"content": "fix the litellm config"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "editing values.yaml"}]}},
            {"type": "user", "message": {"content": "<task-notification>ignore me</task-notification>"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "done, pushing to dev"}]}},
        ]
        path.write_text("\n".join(json.dumps(x) for x in lines))

    def test_derive_from_transcript(self, tmp_path):
        from hooks.context import agent_pool

        t = tmp_path / "t.jsonl"
        self._write_transcript(t)
        with patch.object(agent_pool, "_transcript_path", return_value=t):
            summary = agent_pool._derive_summary_from_transcript("SID", "/x")
        assert "fix the litellm config" in summary  # last real user prompt
        assert "done, pushing to dev" in summary  # last assistant text
        assert "task-notification" not in summary  # envelope skipped

    def test_set_summary_requires_registration(self):
        from hooks.context.agent_pool import set_summary

        assert set_summary("UNKNOWN", "hi") is False

    def test_set_summary_and_list_pool(self):
        from hooks.context.agent_pool import list_pool, set_summary
        from hooks.context.broadcast import register_session

        register_session("SID-A", pid=os.getpid(), cwd="/work/a", model="sonnet")
        assert set_summary("SID-A", "rolling out litellm") is True

        peers = list_pool(include_self="SID-OTHER")
        entry = next(p for p in peers if p["session_id"] == "SID-A")
        assert entry["summary"] == "rolling out litellm"

    def test_list_pool_excludes_self(self):
        from hooks.context.agent_pool import list_pool
        from hooks.context.broadcast import register_session

        register_session("SID-SELF", pid=os.getpid(), cwd="/w", model="m")
        assert all(p["session_id"] != "SID-SELF" for p in list_pool(include_self="SID-SELF"))


# ---------------------------------------------------------------------------
# call_agent routing
# ---------------------------------------------------------------------------


class TestCallAgent:
    def test_unknown_target_errors(self):
        from hooks.context.agent_pool import call_agent

        res = call_agent("GHOST", "hi", caller_session_id="ME")
        assert res["success"] is False
        assert "pool" in res["error"]

    def test_cannot_call_self(self):
        from hooks.context.agent_pool import call_agent

        res = call_agent("ME", "hi", caller_session_id="ME")
        assert res["success"] is False

    def test_live_peer_forks_and_notifies(self):
        from hooks.context import agent_pool
        from hooks.context.broadcast import get_pending_broadcasts, register_session

        register_session("PEER", pid=os.getpid(), cwd="/work/peer", model="sonnet")  # alive → live

        with patch.object(agent_pool, "_run_claude", return_value=(0, "mid-rollout on litellm", "FORKSID")):
            res = agent_pool.call_agent("PEER", "are you on litellm?", caller_session_id="ME")

        assert res["success"] is True
        assert res["mode"] == "forked+notified"
        assert res["delivered"] is True
        assert res["their_state"] == "mid-rollout on litellm"
        # The live peer received a directed inbox message it will see next turn.
        peer_inbox = [m["message"] for m in get_pending_broadcasts("PEER")]
        assert any("are you on litellm?" in m for m in peer_inbox)

    def test_dormant_peer_resumes(self):
        from hooks.context import agent_pool
        from hooks.context.broadcast import register_session

        register_session("PEER", pid=999_999_999, cwd="/work/peer", model="sonnet")  # dead → dormant

        with (
            patch.object(agent_pool, "_transcript_recent", return_value=False),
            patch.object(agent_pool, "_run_claude", return_value=(0, "I finished the deploy", "")),
        ):
            res = agent_pool.call_agent("PEER", "status?", caller_session_id="ME")

        assert res["success"] is True
        assert res["mode"] == "resumed"
        assert res["reply"] == "I finished the deploy"

    def test_missing_cwd_errors(self):
        from hooks.context import agent_pool
        from hooks.context.broadcast import _load_sessions, _save_sessions

        sessions = _load_sessions()
        sessions["NOCWD"] = {"status": "alive", "pid": os.getpid(), "cwd": "", "model": "m"}
        _save_sessions(sessions)

        res = agent_pool.call_agent("NOCWD", "hi", caller_session_id="ME")
        assert res["success"] is False
        assert "cwd" in res["error"]
