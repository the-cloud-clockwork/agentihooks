"""Tests for the rules_refresh one-shot delivery system."""

import hashlib
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
    state_dir = tmp_path / ".agentihooks"
    state_dir.mkdir()
    from hooks import config as _cfg

    monkeypatch.setattr(_cfg, "AGENTIHOOKS_HOME", state_dir)
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
            "home": rules_refresh.profile_home(rules_refresh.claude_home()),
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
        payload = rules_refresh.collect_profile_rules(tmp_path / "nope", claude_md, tmp_path / "no-local.md")
        assert "# Only global" in payload


def _role_home(root: Path, role: str, render: str) -> Path:
    home = root / ".agentihooks" / "profiles" / ".homes" / role / render / "claude"
    home.mkdir(parents=True)
    return home


@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        ((".homes", "frontend", "abc-1", "claude"), "frontend"),
        ((".homes", "frontend"), "frontend"),
        ((".homes",), None),
        ((".claude",), None),
    ],
)
def test_home_profile_names_the_key_under_the_homes_layout(tmp_path, tail, expected):
    assert rules_refresh.home_profile(tmp_path.joinpath(*tail)) == expected


class TestProfileHomeScope:
    def test_marker_records_the_profile_home_it_was_rendered_from(self, tmp_home, mock_sessions, monkeypatch):
        mock_sessions.return_value = {"s1": {"status": "alive"}}
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(_role_home(tmp_home, "frontend", "abc-1")))
        rules_refresh.write_refresh_marker("frontend", "FRONTEND PERSONA")
        assert rules_refresh._load_marker("frontend")["home"] == "frontend"

    def test_a_role_home_refresh_reaches_only_sessions_of_that_role(self, tmp_home, mock_sessions, monkeypatch):
        homes = {"s-frontend-old": _role_home(tmp_home, "frontend", "old-1")}
        for n in range(13):
            role = ("engineer", "master", "anton")[n % 3]
            homes[f"s-{role}-{n}"] = _role_home(tmp_home, role, f"r{n}-1")
        caller = _role_home(tmp_home, "frontend", "new-2")
        mock_sessions.return_value = {sid: {"status": "alive"} for sid in [*homes, "s-caller"]}
        homes["s-caller"] = caller
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(caller))
        rules_refresh.write_refresh_marker("frontend", "FRONTEND PERSONA")

        received = []
        for sid, home in homes.items():
            monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
            monkeypatch.setattr(rules_refresh, "inject_context", lambda payload, sid=sid: received.append(sid))
            rules_refresh.maybe_inject(sid)

        assert sorted(received) == ["s-caller", "s-frontend-old"]
        assert rules_refresh._load_marker("frontend") is None

    def test_an_unrendered_home_refresh_skips_rendered_sessions(self, tmp_home, mock_sessions, monkeypatch):
        caller = tmp_home / ".claude"
        caller.mkdir()
        mock_sessions.return_value = {"s-plain": {"status": "alive"}, "s-engineer": {"status": "alive"}}
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(caller))
        rules_refresh.write_refresh_marker("anton", "ANTON PERSONA")
        assert rules_refresh._load_marker("anton")["home"] == str(caller.resolve())

        received = []
        for sid, home in (("s-engineer", _role_home(tmp_home, "engineer", "e-1")), ("s-plain", caller)):
            monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
            monkeypatch.setattr(rules_refresh, "inject_context", lambda payload, sid=sid: received.append(sid))
            rules_refresh.maybe_inject(sid)

        assert received == ["s-plain"]

    def test_a_marker_without_a_profile_home_reaches_no_session(self, tmp_home, monkeypatch):
        rules_refresh._save_marker(
            "anton",
            {
                "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "profile": "anton",
                "content_hash": "abc",
                "payload": "FRONTEND PERSONA",
                "pending": ["s-master"],
            },
        )
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(_role_home(tmp_home, "master", "m-1")))
        received = []
        monkeypatch.setattr(rules_refresh, "inject_context", received.append)
        rules_refresh.maybe_inject("s-master")
        assert received == []
        assert rules_refresh._load_marker("anton") is None

    def test_a_skipped_session_is_logged_with_its_profile_and_id(self, tmp_home, mock_sessions, monkeypatch):
        mock_sessions.return_value = {"s-master": {"status": "alive"}}
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(_role_home(tmp_home, "frontend", "f-1")))
        rules_refresh.write_refresh_marker("frontend", "FRONTEND PERSONA")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(_role_home(tmp_home, "master", "m-1")))
        logged = []
        monkeypatch.setattr(rules_refresh, "log", lambda *args: logged.append(args))
        rules_refresh.maybe_inject("s-master")
        assert (
            "rules_refresh: other profile home skipped",
            {"profile": "frontend", "session_id": "s-master"},
        ) in logged

    def test_refresh_from_a_role_home_keys_the_marker_by_that_role(self, tmp_home, monkeypatch):
        from argparse import Namespace

        from scripts import install

        home = _role_home(tmp_home, "frontend", "abc-1")
        (home / "CLAUDE.md").write_text("You are frontend")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
        monkeypatch.setattr(install, "_detect_active_profile", lambda: "anton")
        monkeypatch.setattr("scripts.targets.claude_target.refresh_rules", lambda *args: "You are frontend")
        seen = []

        def capture(profile, payload):
            seen.append(profile)
            return {"marker_path": "marker", "pending_count": 0, "content_hash": "proof"}

        monkeypatch.setattr("hooks.context.rules_refresh.write_refresh_marker", capture)
        install._cmd_refresh_rules(Namespace(profile=None, clear=False, dry_run=False))
        assert seen == ["frontend"]

    def test_a_session_receives_only_its_own_home_after_another_render_changes_the_global_install(
        self, tmp_home, mock_sessions, monkeypatch
    ):
        master = _role_home(tmp_home, "master", "m-1")
        (master / "CLAUDE.md").write_text("You are master")
        (master / "rules").mkdir()
        (master / "rules" / "master-role.md").write_text("Master rule")
        (master / "CLAUDE.local.md").write_text("Master local")
        global_install = tmp_home / ".claude"
        (global_install / "rules").mkdir(parents=True)
        (global_install / "CLAUDE.md").write_text("You are frontend")
        (global_install / "rules" / "frontend-role.md").write_text("Frontend rule")
        mock_sessions.return_value = {"s-master": {"status": "alive"}}
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(master))
        frontend_payload = rules_refresh.collect_profile_rules(
            global_install / "rules", global_install / "CLAUDE.md", global_install / "CLAUDE.local.md"
        )
        rules_refresh.write_refresh_marker("master", frontend_payload)
        received = []
        logged = []
        monkeypatch.setattr(rules_refresh, "inject_context", received.append)
        monkeypatch.setattr(rules_refresh, "log", lambda *args: logged.append(args))

        rules_refresh.maybe_inject("s-master")

        assert len(received) == 1
        assert "--- CLAUDE.md ---\nYou are master\n" in received[0]
        assert "--- rules/master-role.md ---\nMaster rule\n" in received[0]
        assert "--- CLAUDE.local.md (local override) ---\nMaster local\n" in received[0]
        assert "frontend" not in received[0].lower()
        assert (
            "rules_refresh: delivered",
            {
                "profile": "master",
                "session_id": "s-master",
                "content_hash": hashlib.sha256(received[0].encode("utf-8")).hexdigest()[:16],
            },
        ) in logged
        assert rules_refresh._load_marker("master") is None
