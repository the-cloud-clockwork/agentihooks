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
    def test_alive_claude_pid_is_live(self):
        from hooks.context import agent_pool

        # A live pid that IS a claude process → live.
        with patch.object(agent_pool, "_pid_is_claude", return_value=True):
            assert agent_pool.is_peer_live("SID", {"pid": os.getpid(), "cwd": "/nope"}) is True

    def test_alive_pid_but_not_claude_is_not_live(self):
        from hooks.context import agent_pool

        # pid-reuse: the pid is alive but belongs to an unrelated process → NOT
        # live (this is the false-positive the /proc/comm guard closes).
        with patch.object(agent_pool, "_pid_is_claude", return_value=False):
            assert agent_pool.is_peer_live("SID", {"pid": os.getpid(), "cwd": "/nope"}) is False

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

    def test_disabled_returns_early(self):
        from hooks.context import agent_pool

        with patch.object(agent_pool, "AGENT_POOL_ENABLED", False):
            res = agent_pool.call_agent("PEER", "hi", caller_session_id="ME")
        assert res["success"] is False

    def test_rejects_non_alive_status(self):
        from hooks.context import agent_pool
        from hooks.context.broadcast import _load_sessions, _save_sessions

        sessions = _load_sessions()
        sessions["SUPER"] = {"status": "superseded", "pid": os.getpid(), "cwd": "/w", "model": "m"}
        _save_sessions(sessions)
        # Must NOT spawn a subprocess for a phantom target.
        with patch.object(agent_pool, "_run_claude", side_effect=AssertionError("must not run")):
            res = agent_pool.call_agent("SUPER", "hi", caller_session_id="ME")
        assert res["success"] is False
        assert "not alive" in res["error"]

    def test_live_peer_forks_and_notifies(self):
        from hooks.context import agent_pool
        from hooks.context.broadcast import get_pending_broadcasts, register_session

        register_session("PEER", pid=os.getpid(), cwd="/work/peer", model="sonnet")

        calls = {}

        def _capture(extra_args, prompt, cwd):
            calls["extra_args"] = extra_args
            return (0, "mid-rollout on litellm", "FORKSID")

        with (
            patch.object(agent_pool, "is_peer_live", return_value=True),
            patch.object(agent_pool, "_run_claude", side_effect=_capture),
        ):
            res = agent_pool.call_agent("PEER", "are you on litellm?", caller_session_id="ME")

        assert res["success"] is True
        assert res["mode"] == "live"
        assert res["delivered"] is True
        assert res["their_state"] == "mid-rollout on litellm"
        # The read is ALWAYS a fork — never an un-forked --resume.
        assert "--fork-session" in calls["extra_args"]
        # Directed inbox reached the peer.
        peer_inbox = [m["message"] for m in get_pending_broadcasts("PEER")]
        assert any("are you on litellm?" in m for m in peer_inbox)

    def test_dormant_peer_also_forks_never_unforked_resume(self):
        """The dormant path must ALSO fork — never an un-forked --resume (the
        corruption/identity-laundering vector)."""
        from hooks.context import agent_pool
        from hooks.context.broadcast import register_session

        register_session("PEER", pid=999_999_999, cwd="/work/peer", model="sonnet")

        calls = {}

        def _capture(extra_args, prompt, cwd):
            calls["extra_args"] = extra_args
            return (0, "I finished the deploy", "FORKSID2")

        with (
            patch.object(agent_pool, "is_peer_live", return_value=False),
            patch.object(agent_pool, "_run_claude", side_effect=_capture),
        ):
            res = agent_pool.call_agent("PEER", "status?", caller_session_id="ME")

        assert res["success"] is True
        assert res["mode"] == "dormant"
        assert res["their_state"] == "I finished the deploy"
        assert "--fork-session" in calls["extra_args"]  # the safety invariant

    def test_missing_cwd_errors(self):
        from hooks.context import agent_pool
        from hooks.context.broadcast import _load_sessions, _save_sessions

        sessions = _load_sessions()
        sessions["NOCWD"] = {"status": "alive", "pid": os.getpid(), "cwd": "", "model": "m"}
        _save_sessions(sessions)

        res = agent_pool.call_agent("NOCWD", "hi", caller_session_id="ME")
        assert res["success"] is False
        assert "cwd" in res["error"]


class TestRunClaude:
    """The subprocess construction is the safety boundary — assert its argv."""

    def _fake_completed(self, stdout):
        class R:
            returncode = 0

        r = R()
        r.stdout = stdout
        return r

    def test_builds_bare_fork_and_disallowed_tools(self):
        from hooks.context import agent_pool

        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return self._fake_completed('{"result": "hi", "session_id": "NEWFORK"}')

        with patch.object(agent_pool.subprocess, "run", side_effect=fake_run):
            rc, text, sid = agent_pool._run_claude(["--resume", "SID", "--fork-session"], "prompt", "/w")

        cmd = seen["cmd"]
        assert "--bare" in cmd  # the load-bearing no-MCP/no-tools flag
        assert "--fork-session" in cmd
        assert "--resume" in cmd
        assert "--disallowedTools" in cmd
        assert agent_pool._NO_ACTION_TOOLS in cmd
        assert "json" in cmd  # --output-format json
        assert rc == 0 and text == "hi" and sid == "NEWFORK"

    def test_parses_non_json_stdout_as_raw(self):
        from hooks.context import agent_pool

        with patch.object(agent_pool.subprocess, "run", return_value=self._fake_completed("plain text reply")):
            rc, text, sid = agent_pool._run_claude(["--resume", "SID", "--fork-session"], "p", "/w")
        assert text == "plain text reply"
        assert sid == ""


class TestCleanupFork:
    def test_deletes_new_fork_transcript(self, tmp_path):
        from hooks.context import agent_pool

        fork_file = tmp_path / "FORK.jsonl"
        fork_file.write_text("{}\n")
        with patch.object(agent_pool, "_transcript_path", return_value=fork_file):
            agent_pool._cleanup_fork("/w", "FORK", "ORIGINAL")
        assert not fork_file.exists()

    def test_never_deletes_when_sid_equals_original(self, tmp_path):
        from hooks.context import agent_pool

        real_file = tmp_path / "ORIGINAL.jsonl"
        real_file.write_text("{}\n")
        called = {"n": 0}

        def _tp(sid, cwd):
            called["n"] += 1
            return real_file

        with patch.object(agent_pool, "_transcript_path", side_effect=_tp):
            agent_pool._cleanup_fork("/w", "ORIGINAL", "ORIGINAL")
        # Guard short-circuits before ever resolving the path — original untouched.
        assert real_file.exists()
        assert called["n"] == 0


class TestDirectedPreToolDelivery:
    def test_directed_message_injects_at_pretool_regardless_of_gate(self, monkeypatch):
        from hooks.context import broadcast

        # critical-on-pretool OFF (default) — directed messages must still qualify.
        monkeypatch.setattr(broadcast, "BROADCAST_CRITICAL_ON_PRETOOL", False, raising=False)
        broadcast.create_broadcast("back off", severity="alert", target_session="SID-A")

        ids_a = [m["id"] for m in broadcast.get_pretool_broadcasts("SID-A")]
        ids_b = [m["id"] for m in broadcast.get_pretool_broadcasts("SID-B")]
        assert len(ids_a) == 1  # target sees it at PreToolUse
        assert ids_b == []  # non-target does not

    def test_non_directed_alert_gated_off_at_pretool(self, monkeypatch):
        import hooks.config as cfg
        from hooks.context import broadcast

        monkeypatch.setattr(broadcast, "BROADCAST_CRITICAL_ON_PRETOOL", False, raising=False)
        monkeypatch.setattr(cfg, "BROADCAST_CRITICAL_ON_PRETOOL", False, raising=False)
        broadcast.create_broadcast("routine", severity="alert")  # global, non-directed
        assert broadcast.get_pretool_broadcasts("ANY") == []


class TestSummaryCadenceAndSticky:
    def test_refresh_fires_on_first_and_every_nth(self, monkeypatch):
        from hooks.context import agent_pool
        from hooks.context.broadcast import register_session

        register_session("SID", pid=os.getpid(), cwd="/w", model="m")
        monkeypatch.setattr(agent_pool, "AGENT_POOL_SUMMARY_INTERVAL", 5, raising=False)
        fired = []
        monkeypatch.setattr(
            agent_pool,
            "_derive_summary_from_transcript",
            lambda sid, cwd: fired.append(1) or "derived",
        )
        for _ in range(11):
            agent_pool.maybe_refresh_summary("SID")
        # calls 1,5,10 → 3 derives
        assert len(fired) == 3

    def test_sticky_summary_not_clobbered(self, monkeypatch):
        from hooks.context import agent_pool
        from hooks.context.broadcast import _load_sessions, register_session

        register_session("SID", pid=os.getpid(), cwd="/w", model="m")
        agent_pool.set_summary("SID", "back off, mid-rollout", sticky=True)
        monkeypatch.setattr(agent_pool, "AGENT_POOL_SUMMARY_INTERVAL", 1, raising=False)
        monkeypatch.setattr(agent_pool, "_derive_summary_from_transcript", lambda sid, cwd: "AUTO")
        for _ in range(5):
            agent_pool.maybe_refresh_summary("SID")
        assert _load_sessions()["SID"]["summary"] == "back off, mid-rollout"

    def test_empty_pool_status_clears_sticky(self):
        from hooks.context import agent_pool
        from hooks.context.broadcast import _load_sessions, register_session

        register_session("SID", pid=os.getpid(), cwd="/w", model="m")
        agent_pool.set_summary("SID", "pinned", sticky=True)
        assert _load_sessions()["SID"].get("summary_sticky") is True
        agent_pool.set_summary("SID", "", sticky=False)
        assert "summary_sticky" not in _load_sessions()["SID"]
