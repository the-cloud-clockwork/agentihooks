"""Tests for scripts/sync_daemon.py — file discovery, hashing, change detection, propagation."""

import json
import os

# Import the daemon module under test
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)
import sync_daemon

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def profiles_dir(tmp_path):
    """Create a minimal profiles directory structure."""
    base = tmp_path / "profiles" / "_base"
    base.mkdir(parents=True)
    (base / "settings.base.json").write_text('{"hooks": []}')

    for name in ["default", "coding"]:
        p = tmp_path / "profiles" / name
        p.mkdir(parents=True)
        (p / "profile.yml").write_text(f"name: {name}\nmcp_categories: all\n")
        (p / "settings.overrides.json").write_text("{}")
        claude_dir = p / ".claude"
        claude_dir.mkdir()
        (claude_dir / "CLAUDE.md").write_text(f"# {name} profile")

    return tmp_path / "profiles"


@pytest.fixture
def state_dir(tmp_path):
    """Create a minimal agentihooks state directory."""
    d = tmp_path / "agentihooks_state"
    d.mkdir()
    (d / ".env").write_text("TOKEN_CONTROL_ENABLED=1\n")
    return d


# ---------------------------------------------------------------------------
# TestCollectSourceFiles
# ---------------------------------------------------------------------------


class TestCollectSourceFiles:
    def test_discovers_base_settings(self, profiles_dir):
        with patch.object(sync_daemon, "PROFILES_DIR", profiles_dir):
            files = sync_daemon._collect_source_files({})
        base_path = str(profiles_dir / "_base" / "settings.base.json")
        assert base_path in files
        assert "base" in files[base_path]

    def test_discovers_profile_files(self, profiles_dir):
        with patch.object(sync_daemon, "PROFILES_DIR", profiles_dir):
            files = sync_daemon._collect_source_files({})
        yml_path = str(profiles_dir / "default" / "profile.yml")
        assert yml_path in files
        assert "profile:default" in files[yml_path]

    def test_discovers_multiple_profiles(self, profiles_dir):
        with patch.object(sync_daemon, "PROFILES_DIR", profiles_dir):
            files = sync_daemon._collect_source_files({})
        coding_yml = str(profiles_dir / "coding" / "profile.yml")
        assert coding_yml in files
        assert "profile:coding" in files[coding_yml]

    def test_discovers_mcp_files(self, tmp_path):
        mcp_file = tmp_path / "test.json"
        mcp_file.write_text('{"mcpServers": {}}')
        state = {"mcpFiles": [str(mcp_file)]}

        with patch.object(sync_daemon, "PROFILES_DIR", tmp_path / "empty"):
            with patch.object(sync_daemon, "AGENTIHOOKS_STATE_DIR", tmp_path / "empty"):
                files = sync_daemon._collect_source_files(state)

        assert str(mcp_file) in files
        assert "mcp_files" in files[str(mcp_file)]

    def test_discovers_env_files(self, state_dir):
        with patch.object(sync_daemon, "PROFILES_DIR", state_dir / "empty"):
            with patch.object(sync_daemon, "AGENTIHOOKS_STATE_DIR", state_dir):
                files = sync_daemon._collect_source_files({})
        env_path = str(state_dir / ".env")
        assert env_path in files
        assert "env" in files[env_path]

    def test_missing_files_skipped(self):
        state = {"mcpFiles": ["/nonexistent/path.json"]}
        with patch.object(sync_daemon, "PROFILES_DIR", Path("/nonexistent")):
            with patch.object(sync_daemon, "AGENTIHOOKS_STATE_DIR", Path("/nonexistent")):
                files = sync_daemon._collect_source_files(state)
        assert "/nonexistent/path.json" not in files

    def test_discovers_connector_files(self, tmp_path):
        conn_dir = tmp_path / "my-connector"
        conn_dir.mkdir()
        (conn_dir / "connector.yml").write_text("name: my-connector\n")
        profs = conn_dir / "profiles" / "default"
        profs.mkdir(parents=True)
        (profs / "permissions.json").write_text("{}")

        state = {"connectors": {"my-connector": {"path": str(conn_dir)}}}
        with patch.object(sync_daemon, "PROFILES_DIR", tmp_path / "empty"):
            with patch.object(sync_daemon, "AGENTIHOOKS_STATE_DIR", tmp_path / "empty"):
                files = sync_daemon._collect_source_files(state)

        yml_path = str(conn_dir / "connector.yml")
        assert yml_path in files
        assert "connector:my-connector" in files[yml_path]

    def test_discovers_bundle_files(self, tmp_path):
        bundle = tmp_path / "bundle"
        bp = bundle / "profiles" / "custom"
        bp.mkdir(parents=True)
        (bp / "profile.yml").write_text("name: custom\n")

        state = {"bundle": {"path": str(bundle)}}
        with patch.object(sync_daemon, "PROFILES_DIR", tmp_path / "empty"):
            with patch.object(sync_daemon, "AGENTIHOOKS_STATE_DIR", tmp_path / "empty"):
                files = sync_daemon._collect_source_files(state)

        yml_path = str(bp / "profile.yml")
        assert yml_path in files
        assert "bundle" in files[yml_path]
        assert "profile:custom" in files[yml_path]


# ---------------------------------------------------------------------------
# TestHashComparison
# ---------------------------------------------------------------------------


class TestHashComparison:
    def test_no_changes(self):
        h = {"a": "abc", "b": "def"}
        changed, added, removed = sync_daemon._diff_hashes(h, h.copy())
        assert changed == []
        assert added == []
        assert removed == []

    def test_changed_file(self):
        old = {"a": "abc"}
        new = {"a": "xyz"}
        changed, added, removed = sync_daemon._diff_hashes(old, new)
        assert changed == ["a"]
        assert added == []
        assert removed == []

    def test_added_file(self):
        old = {"a": "abc"}
        new = {"a": "abc", "b": "def"}
        changed, added, removed = sync_daemon._diff_hashes(old, new)
        assert changed == []
        assert added == ["b"]
        assert removed == []

    def test_removed_file(self):
        old = {"a": "abc", "b": "def"}
        new = {"a": "abc"}
        changed, added, removed = sync_daemon._diff_hashes(old, new)
        assert changed == []
        assert added == []
        assert removed == ["b"]

    def test_mixed_changes(self):
        old = {"a": "1", "b": "2", "c": "3"}
        new = {"a": "1", "b": "changed", "d": "4"}
        changed, added, removed = sync_daemon._diff_hashes(old, new)
        assert changed == ["b"]
        assert added == ["d"]
        assert removed == ["c"]


# ---------------------------------------------------------------------------
# TestDetermineAffectedCategories
# ---------------------------------------------------------------------------


class TestDetermineAffectedCategories:
    def test_single_file_single_category(self):
        source_map = {"/a/settings.base.json": ["base"]}
        result = sync_daemon._determine_affected_categories(["/a/settings.base.json"], source_map)
        assert result == {"base"}

    def test_file_with_multiple_categories(self):
        source_map = {"/a/profile.yml": ["profile:default", "bundle"]}
        result = sync_daemon._determine_affected_categories(["/a/profile.yml"], source_map)
        assert result == {"profile:default", "bundle"}

    def test_unknown_file_no_categories(self):
        result = sync_daemon._determine_affected_categories(["/unknown"], {})
        assert result == set()

    def test_multiple_files(self):
        source_map = {
            "/a": ["base"],
            "/b": ["profile:coding"],
        }
        result = sync_daemon._determine_affected_categories(["/a", "/b"], source_map)
        assert result == {"base", "profile:coding"}

    def test_removed_file_with_old_map(self):
        old_map = {"/deleted": ["profile:default", "bundle"]}
        result = sync_daemon._determine_affected_categories(
            [],
            {},
            removed_files=["/deleted"],
            old_source_map=old_map,
        )
        assert result == {"profile:default", "bundle"}

    def test_removed_file_without_old_map_falls_back_to_base(self):
        result = sync_daemon._determine_affected_categories(
            [],
            {},
            removed_files=["/deleted"],
        )
        assert result == {"base"}


# ---------------------------------------------------------------------------
# TestDetermineActions
# ---------------------------------------------------------------------------


class TestDetermineActions:
    @pytest.fixture
    def state_with_targets(self):
        return {
            "targets": {
                "global": {"path": "/home/user/.claude", "profile": "default"},
                "projects": {
                    "/proj/a": {"profile": "default"},
                    "/proj/b": {"profile": "coding"},
                },
            }
        }

    def test_base_change_triggers_everything(self, state_with_targets):
        actions = sync_daemon._determine_actions({"base"}, state_with_targets)
        assert actions["reinstall_global"] is True
        assert set(actions["reinstall_projects"]) == {"/proj/a", "/proj/b"}
        assert actions["sync_mcp"] is True

    def test_bundle_change_triggers_everything(self, state_with_targets):
        actions = sync_daemon._determine_actions({"bundle"}, state_with_targets)
        assert actions["reinstall_global"] is True
        assert len(actions["reinstall_projects"]) == 2

    def test_env_change_triggers_everything(self, state_with_targets):
        actions = sync_daemon._determine_actions({"env"}, state_with_targets)
        assert actions["reinstall_global"] is True
        assert len(actions["reinstall_projects"]) == 2

    def test_profile_change_triggers_matching_targets(self, state_with_targets):
        actions = sync_daemon._determine_actions({"profile:coding"}, state_with_targets)
        assert actions["reinstall_global"] is False  # global uses "default"
        assert actions["reinstall_projects"] == ["/proj/b"]

    def test_profile_change_triggers_global_if_matching(self, state_with_targets):
        actions = sync_daemon._determine_actions({"profile:default"}, state_with_targets)
        assert actions["reinstall_global"] is True
        assert "/proj/a" in actions["reinstall_projects"]
        assert "/proj/b" not in actions["reinstall_projects"]

    def test_connector_change_triggers_all_targets(self, state_with_targets):
        actions = sync_daemon._determine_actions({"connector:my-conn"}, state_with_targets)
        assert actions["reinstall_global"] is True
        assert len(actions["reinstall_projects"]) == 2

    def test_mcp_change_triggers_sync_only(self, state_with_targets):
        actions = sync_daemon._determine_actions({"mcp_files"}, state_with_targets)
        assert actions["reinstall_global"] is False
        assert actions["reinstall_projects"] == []
        assert actions["sync_mcp"] is True

    def test_no_targets_registered(self):
        actions = sync_daemon._determine_actions({"base"}, {})
        assert actions["reinstall_global"] is False
        assert actions["reinstall_projects"] == []
        assert actions["sync_mcp"] is True  # base triggers sync too

    def test_empty_categories(self, state_with_targets):
        actions = sync_daemon._determine_actions(set(), state_with_targets)
        assert actions["reinstall_global"] is False
        assert actions["reinstall_projects"] == []
        assert actions["sync_mcp"] is False


# ---------------------------------------------------------------------------
# TestTargetRegistration (in install.py)
# ---------------------------------------------------------------------------


class TestTargetRegistration:
    @pytest.fixture(autouse=True)
    def _import_install(self):
        scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import install

        self.install = install

    def test_register_global(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text("{}")

        original_state = self.install.STATE_JSON
        original_dir = self.install.AGENTIHOOKS_STATE_DIR
        try:
            self.install.STATE_JSON = state_file
            self.install.AGENTIHOOKS_STATE_DIR = tmp_path
            self.install._register_target_global("default")
        finally:
            self.install.STATE_JSON = original_state
            self.install.AGENTIHOOKS_STATE_DIR = original_dir

        state = json.loads(state_file.read_text())
        assert "targets" in state
        assert state["targets"]["global"]["profile"] == "default"
        assert "installed_at" in state["targets"]["global"]

    def test_register_project(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text("{}")

        original_state = self.install.STATE_JSON
        original_dir = self.install.AGENTIHOOKS_STATE_DIR
        try:
            self.install.STATE_JSON = state_file
            self.install.AGENTIHOOKS_STATE_DIR = tmp_path
            self.install._register_target_project(Path("/my/project"), "coding")
        finally:
            self.install.STATE_JSON = original_state
            self.install.AGENTIHOOKS_STATE_DIR = original_dir

        state = json.loads(state_file.read_text())
        assert "/my/project" in state["targets"]["projects"]
        assert state["targets"]["projects"]["/my/project"]["profile"] == "coding"

    def test_unregister_project(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"targets": {"projects": {"/my/project": {"profile": "default"}}}}))

        original_state = self.install.STATE_JSON
        original_dir = self.install.AGENTIHOOKS_STATE_DIR
        try:
            self.install.STATE_JSON = state_file
            self.install.AGENTIHOOKS_STATE_DIR = tmp_path
            self.install._unregister_target_project(Path("/my/project"))
        finally:
            self.install.STATE_JSON = original_state
            self.install.AGENTIHOOKS_STATE_DIR = original_dir

        state = json.loads(state_file.read_text())
        assert "/my/project" not in state["targets"]["projects"]


# ---------------------------------------------------------------------------
# TestSha256
# ---------------------------------------------------------------------------


class TestSha256:
    def test_hashes_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        result = sync_daemon._sha256(f)
        assert result is not None
        assert len(result) == 64  # SHA-256 hex digest length

    def test_returns_none_for_missing(self):
        result = sync_daemon._sha256(Path("/nonexistent"))
        assert result is None

    def test_different_content_different_hash(self, tmp_path):
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("hello")
        f2.write_text("world")
        assert sync_daemon._sha256(f1) != sync_daemon._sha256(f2)

    def test_same_content_same_hash(self, tmp_path):
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("same")
        f2.write_text("same")
        assert sync_daemon._sha256(f1) == sync_daemon._sha256(f2)


# ---------------------------------------------------------------------------
# C1 — Recursive content hash
# ---------------------------------------------------------------------------


class TestDirContentHash:
    def test_returns_none_for_missing_dir(self, tmp_path):
        assert sync_daemon._dir_content_hash(tmp_path / "nope") is None

    def test_detects_content_change_in_nested_file(self, tmp_path):
        d = tmp_path / "skill"
        (d / "sub").mkdir(parents=True)
        f = d / "sub" / "SKILL.md"
        f.write_text("v1")
        h1 = sync_daemon._dir_content_hash(d)
        f.write_text("v2")
        h2 = sync_daemon._dir_content_hash(d)
        assert h1 != h2

    def test_stable_for_same_content_after_mtime_touch(self, tmp_path):
        d = tmp_path / "skill"
        d.mkdir()
        f = d / "SKILL.md"
        f.write_text("body")
        h1 = sync_daemon._dir_content_hash(d)
        f.touch()
        h2 = sync_daemon._dir_content_hash(d)
        assert h1 == h2

    def test_skips_dotfiles(self, tmp_path):
        d = tmp_path / "skill"
        d.mkdir()
        (d / "real.md").write_text("body")
        h_before = sync_daemon._dir_content_hash(d)
        (d / ".hidden").write_text("ignored")
        h_after = sync_daemon._dir_content_hash(d)
        assert h_before == h_after


# ---------------------------------------------------------------------------
# M1 — Carry forward on transient read failure
# ---------------------------------------------------------------------------


class TestComputeHashesCarryForward:
    def test_carries_forward_on_transient_failure(self, tmp_path, monkeypatch):
        f = tmp_path / "watched.json"
        f.write_text("{}")
        previous = {str(f): "deadbeef" * 8}
        monkeypatch.setattr(sync_daemon, "_sha256", lambda p: None)
        failure_counts: dict[str, int] = {}
        result = sync_daemon._compute_hashes(
            {str(f): ["base"]},
            previous_hashes=previous,
            failure_counts=failure_counts,
        )
        assert result[str(f)] == "deadbeef" * 8
        assert failure_counts[str(f)] == 1

    def test_drops_after_threshold(self, tmp_path, monkeypatch):
        f = tmp_path / "watched.json"
        f.write_text("{}")
        previous = {str(f): "abc"}
        monkeypatch.setattr(sync_daemon, "_sha256", lambda p: None)
        failure_counts = {str(f): 2}
        result = sync_daemon._compute_hashes(
            {str(f): ["base"]},
            previous_hashes=previous,
            failure_counts=failure_counts,
            failure_threshold=3,
        )
        assert str(f) not in result
        assert failure_counts[str(f)] == 3


# ---------------------------------------------------------------------------
# C3 — Strict JSON loader + corruption logging
# ---------------------------------------------------------------------------


class TestLoadJsonStrict:
    def test_returns_empty_for_missing(self, tmp_path):
        assert sync_daemon._load_json_strict(tmp_path / "nope.json") == {}

    def test_raises_on_malformed(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("{ this is not json")
        with pytest.raises(sync_daemon.CorruptStateError):
            sync_daemon._load_json_strict(f)

    def test_returns_data_when_valid(self, tmp_path):
        f = tmp_path / "ok.json"
        f.write_text('{"a": 1}')
        assert sync_daemon._load_json_strict(f) == {"a": 1}


class TestClaudeJsonContext:
    def test_load_corrupt_logs_and_returns_empty(self, tmp_path, capsys):
        f = tmp_path / "corrupt.json"
        f.write_text("{not valid")
        ctx = sync_daemon.ClaudeJsonContext.load(f)
        assert ctx.corrupt is True
        assert ctx.data == {}
        out = capsys.readouterr().out
        assert "CORRUPT JSON" in out

    def test_load_valid(self, tmp_path):
        f = tmp_path / "ok.json"
        f.write_text('{"mcpServers": {"x": {}}, "projects": {"/p": {}}}')
        ctx = sync_daemon.ClaudeJsonContext.load(f)
        assert ctx.corrupt is False
        assert "x" in ctx.mcp_servers
        assert "/p" in ctx.projects


# ---------------------------------------------------------------------------
# H2 — New-project grace window
# ---------------------------------------------------------------------------


class TestNewProjectGrace:
    def test_first_sighting_records_and_defers_backfill(self, tmp_path, monkeypatch):
        claude_json = tmp_path / "claude.json"
        known_servers = tmp_path / "known.json"
        known_projects = tmp_path / "known-projects.json"

        claude_json.write_text(json.dumps({"mcpServers": {"srv-a": {}}, "projects": {"/new/proj": {}}}))
        known_servers.write_text(json.dumps({"knownMcpServers": ["srv-a"]}))

        monkeypatch.setattr(sync_daemon, "CLAUDE_JSON", claude_json)
        monkeypatch.setattr(sync_daemon, "KNOWN_PROJECTS_FILE", known_projects)

        import importlib

        scripts_dir = str(Path(sync_daemon.__file__).parent)
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        install = importlib.import_module("install")
        monkeypatch.setattr(install, "_collect_child_enabled_mcps", lambda *a, **kw: set())

        sync_daemon._check_new_projects(known_servers)

        cj = json.loads(claude_json.read_text())
        assert cj["projects"]["/new/proj"] == {}
        ledger = json.loads(known_projects.read_text())
        assert "/new/proj" in ledger["projects"]

    def test_backfill_after_grace(self, tmp_path, monkeypatch):
        claude_json = tmp_path / "claude.json"
        known_servers = tmp_path / "known.json"
        known_projects = tmp_path / "known-projects.json"

        claude_json.write_text(json.dumps({"mcpServers": {"srv-a": {}}, "projects": {"/old/proj": {}}}))
        known_servers.write_text(json.dumps({"knownMcpServers": ["srv-a"]}))

        from datetime import datetime, timedelta, timezone

        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=999)).isoformat()
        known_projects.write_text(json.dumps({"projects": {"/old/proj": old_ts}}))

        monkeypatch.setattr(sync_daemon, "CLAUDE_JSON", claude_json)
        monkeypatch.setattr(sync_daemon, "KNOWN_PROJECTS_FILE", known_projects)

        import importlib

        scripts_dir = str(Path(sync_daemon.__file__).parent)
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        install = importlib.import_module("install")
        monkeypatch.setattr(install, "_collect_child_enabled_mcps", lambda *a, **kw: set())

        sync_daemon._check_new_projects(known_servers)

        cj = json.loads(claude_json.read_text())
        assert cj["projects"]["/old/proj"]["disabledMcpServers"] == ["srv-a"]


# ---------------------------------------------------------------------------
# M3 — Step watchdog
# ---------------------------------------------------------------------------


class TestStepWatchdog:
    def test_returns_value_on_success(self):
        assert sync_daemon._step("ok", lambda: 42, timeout=5) == 42

    def test_returns_default_on_timeout(self):
        import time

        result = sync_daemon._step("slow", lambda: time.sleep(2) or "done", timeout=0.2, default="abandoned")
        assert result == "abandoned"

    def test_returns_default_on_exception(self):
        def boom():
            raise RuntimeError("boom")

        assert sync_daemon._step("err", boom, timeout=5, default=None) is None

    def test_no_timeout_runs_inline(self):
        # timeout=None disables the watchdog — used for legit long-running
        # steps like _execute_actions where 120s would falsely flag a real
        # install as stuck.
        called = []

        def slow():
            called.append("ran")
            return "ok"

        assert sync_daemon._step("inline", slow, timeout=None) == "ok"
        assert called == ["ran"]

    def test_no_timeout_catches_exception(self):
        def boom():
            raise RuntimeError("x")

        assert sync_daemon._step("inline-err", boom, timeout=None, default="fallback") == "fallback"


# ---------------------------------------------------------------------------
# M4 — Heartbeat
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_writes_heartbeat_file(self, tmp_path, monkeypatch):
        hb_file = tmp_path / "hb.json"
        monkeypatch.setattr(sync_daemon, "HEARTBEAT_FILE", hb_file)
        sync_daemon._write_heartbeat(last_success_iso="2026-05-07T00:00:00+00:00", cycles=7)
        data = json.loads(hb_file.read_text())
        assert data["cycles"] == 7
        assert data["last_success"] == "2026-05-07T00:00:00+00:00"
        assert "last_cycle" in data
        assert "version" in data
        assert data["pid"] == os.getpid()
        assert data["failed_cycle_count"] == 0  # default

    def test_persists_failed_cycle_count(self, tmp_path, monkeypatch):
        # Regression: M6 crash-loop bound only works if failed_cycle_count
        # survives daemon restarts via the heartbeat file.
        hb_file = tmp_path / "hb.json"
        monkeypatch.setattr(sync_daemon, "HEARTBEAT_FILE", hb_file)
        sync_daemon._write_heartbeat(last_success_iso=None, cycles=1, failed_cycle_count=2)
        data = json.loads(hb_file.read_text())
        assert data["failed_cycle_count"] == 2


# ---------------------------------------------------------------------------
# M6 — Sentinel
# ---------------------------------------------------------------------------


class TestSentinel:
    def test_write_and_read(self, tmp_path, monkeypatch):
        s_file = tmp_path / "sentinel.json"
        monkeypatch.setattr(sync_daemon, "SENTINEL_FILE", s_file)
        sync_daemon._write_sentinel({"reinstall_global": True, "reinstall_projects": [], "sync_mcp": False}, cycle_id=3)
        data = sync_daemon._read_sentinel()
        assert data is not None
        assert data["cycle_id"] == 3
        assert data["attempted_actions"]["reinstall_global"] is True

    def test_clear_removes_file(self, tmp_path, monkeypatch):
        s_file = tmp_path / "sentinel.json"
        monkeypatch.setattr(sync_daemon, "SENTINEL_FILE", s_file)
        sync_daemon._write_sentinel({}, cycle_id=1)
        assert s_file.exists()
        sync_daemon._clear_sentinel()
        assert not s_file.exists()

    def test_corrupt_returns_corrupt_marker(self, tmp_path, monkeypatch):
        s_file = tmp_path / "sentinel.json"
        s_file.write_text("{not json")
        monkeypatch.setattr(sync_daemon, "SENTINEL_FILE", s_file)
        assert sync_daemon._read_sentinel() == {"corrupt": True}


# ---------------------------------------------------------------------------
# M5 — Prune fallback when valid is empty
# ---------------------------------------------------------------------------


class TestPruneFallback:
    def test_falls_back_to_managed_when_valid_empty(self, tmp_path, monkeypatch):
        claude_json = tmp_path / "claude.json"
        known_servers = tmp_path / "known.json"
        claude_json.write_text(json.dumps({"mcpServers": {}, "projects": {}}))
        known_servers.write_text(json.dumps({"knownMcpServers": ["srv-x"]}))
        monkeypatch.setattr(sync_daemon, "CLAUDE_JSON", claude_json)
        monkeypatch.setattr(sync_daemon, "_get_managed_mcp_names", lambda: {"srv-x"})

        summary = sync_daemon._prune_stale_mcp_servers(known_servers)
        assert summary["pruned_known"] == 0


# ---------------------------------------------------------------------------
# M2 — Connector scoping
# ---------------------------------------------------------------------------


class TestConnectorScoping:
    def test_default_dir_means_all_profiles(self, tmp_path):
        conn = tmp_path / "conn-a"
        (conn / "profiles" / "default").mkdir(parents=True)
        state = {"connectors": {"conn-a": {"path": str(conn)}}}
        assert sync_daemon._connector_scoped_profiles("conn-a", state) is None

    def test_explicit_profiles(self, tmp_path):
        conn = tmp_path / "conn-b"
        (conn / "profiles" / "anton").mkdir(parents=True)
        (conn / "profiles" / "brain").mkdir(parents=True)
        state = {"connectors": {"conn-b": {"path": str(conn)}}}
        assert sync_daemon._connector_scoped_profiles("conn-b", state) == {"anton", "brain"}

    def test_unknown_connector_returns_none(self):
        assert sync_daemon._connector_scoped_profiles("ghost", {"connectors": {}}) is None

    def test_empty_path_returns_none(self):
        # Empty path must not be silently turned into "." by Path("") —
        # the function should treat it as unknown scope.
        state = {"connectors": {"broken": {"path": ""}}}
        assert sync_daemon._connector_scoped_profiles("broken", state) is None
