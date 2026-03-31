"""Tests for scripts/sync_daemon.py — file discovery, hashing, change detection, propagation."""

import json

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
            [], {}, removed_files=["/deleted"], old_source_map=old_map,
        )
        assert result == {"profile:default", "bundle"}

    def test_removed_file_without_old_map_falls_back_to_base(self):
        result = sync_daemon._determine_affected_categories(
            [], {}, removed_files=["/deleted"],
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
        state_file.write_text(json.dumps({
            "targets": {"projects": {"/my/project": {"profile": "default"}}}
        }))

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
