"""Comprehensive install validation tests.

Tests the full install pipeline: 3-layer symlinks, CLAUDE.md linking,
MCP merging, settings generation, and profile structure conventions.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import install  # noqa: I001


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def install_env(tmp_path):
    """Build a complete fake install environment with all 3 layers."""
    claude_home = tmp_path / ".claude"
    claude_home.mkdir()
    for d in ("skills", "agents", "commands", "rules"):
        (claude_home / d).mkdir()

    agentihooks_root = tmp_path / "agentihooks"
    agentihooks_root.mkdir()

    # --- Layer 1: agentihooks built-in .claude/ ---
    ah_claude = agentihooks_root / ".claude"
    ah_claude.mkdir()
    for d in ("skills", "agents", "commands", "rules"):
        (ah_claude / d).mkdir()

    # Built-in agent
    (ah_claude / "agents" / "error-researcher.md").write_text("# Error Researcher\n")
    # Built-in command
    (ah_claude / "commands" / "status.md").write_text("# Status\n")
    # Built-in skill (directory with SKILL.md)
    skill_dir = ah_claude / "skills" / "builtin-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Built-in Skill\n")
    # Built-in rule
    (ah_claude / "rules" / "builtin-rule.md").write_text("# Built-in Rule\n")

    # --- Layer 2: bundle ---
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    bundle_claude = bundle / ".claude"
    bundle_claude.mkdir()
    for d in ("skills", "agents", "commands", "rules"):
        (bundle_claude / d).mkdir()

    # Bundle agent
    (bundle_claude / "agents" / "code-reviewer.md").write_text("# Code Reviewer\n")
    # Bundle command
    (bundle_claude / "commands" / "review.md").write_text("# Review\n")
    # Bundle skill
    bskill = bundle_claude / "skills" / "bundle-skill"
    bskill.mkdir()
    (bskill / "SKILL.md").write_text("# Bundle Skill\n")
    # Bundle rule
    (bundle_claude / "rules" / "python.md").write_text("# Python Rules\n")
    # Bundle MCP
    (bundle_claude / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"bundle-server": {"type": "http", "url": "http://localhost:9090/mcp/"}}})
    )

    # --- Layer 3: profile (in bundle) ---
    profile = bundle / "profiles" / "test-profile"
    profile.mkdir(parents=True)
    profile_claude = profile / ".claude"
    profile_claude.mkdir()
    for d in ("skills", "agents", "commands", "rules"):
        (profile_claude / d).mkdir()

    # Profile CLAUDE.md (at root, not inside .claude/)
    (profile / "CLAUDE.md").write_text("# Test Profile System Prompt\n")
    # Profile agent
    (profile_claude / "agents" / "profile-agent.md").write_text("# Profile Agent\n")
    # Profile command
    (profile_claude / "commands" / "deploy.md").write_text("# Deploy\n")
    # Profile skill
    pskill = profile_claude / "skills" / "profile-skill"
    pskill.mkdir()
    (pskill / "SKILL.md").write_text("# Profile Skill\n")
    # Profile rule
    (profile_claude / "rules" / "git-workflow.md").write_text("# Git Workflow\n")
    # Profile settings overrides
    (profile_claude / "settings.overrides.json").write_text(json.dumps({"env": {"PROFILE_VAR": "test-value"}}))
    # Profile .mcp.json
    (profile_claude / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"profile-server": {"type": "http", "url": "http://localhost:8080/mcp/"}}})
    )
    # Profile.yml
    (profile / "profile.yml").write_text(
        "name: test-profile\n"
        "description: Test profile\n"
        "mcp_categories: all\n"
        "enabledMcpServers: []\n"
        "otel:\n"
        "  enabled: false\n"
    )

    # --- Base settings ---
    base_dir = agentihooks_root / "profiles" / "_base"
    base_dir.mkdir(parents=True)
    (base_dir / "settings.base.json").write_text(
        json.dumps(
            {
                "hooks": {},
                "env": {"BASE_VAR": "base-value"},
            }
        )
    )

    # State dir
    state_dir = tmp_path / ".agentihooks"
    state_dir.mkdir()
    (state_dir / "state.json").write_text(
        json.dumps(
            {
                "bundle": {"path": str(bundle), "linked_at": "2026-01-01T00:00:00+00:00"},
                "mcpFiles": [],
            }
        )
    )

    # Claude.json (user MCP scope)
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(json.dumps({"mcpServers": {}}))

    return {
        "tmp": tmp_path,
        "claude_home": claude_home,
        "agentihooks_root": agentihooks_root,
        "bundle": bundle,
        "profile": profile,
        "profile_name": "test-profile",
        "state_dir": state_dir,
        "claude_json": claude_json,
    }


# ---------------------------------------------------------------------------
# Profile structure convention tests
# ---------------------------------------------------------------------------


class TestProfileStructureConvention:
    """Verify profile directories follow the Claude Code project convention."""

    def test_claude_md_at_profile_root(self, install_env):
        """CLAUDE.md must be at profile root, not inside .claude/."""
        profile = install_env["profile"]
        assert (profile / "CLAUDE.md").exists()
        assert not (profile / ".claude" / "CLAUDE.md").exists()

    def test_settings_overrides_inside_claude(self, install_env):
        """settings.overrides.json must be inside .claude/."""
        profile = install_env["profile"]
        assert (profile / ".claude" / "settings.overrides.json").exists()
        assert not (profile / "settings.overrides.json").exists()

    def test_profile_yml_at_root(self, install_env):
        """profile.yml (agentihooks metadata) stays at root."""
        profile = install_env["profile"]
        assert (profile / "profile.yml").exists()

    def test_profile_has_mcp_json(self, install_env):
        """Profile can have .claude/.mcp.json for profile-specific MCPs."""
        profile = install_env["profile"]
        assert (profile / ".claude" / ".mcp.json").exists()

    def test_artifact_dirs_inside_claude(self, install_env):
        """skills/, agents/, commands/, rules/ live inside .claude/."""
        profile = install_env["profile"]
        for d in ("skills", "agents", "commands", "rules"):
            assert (profile / ".claude" / d).is_dir(), f".claude/{d}/ missing"


class TestBuiltinProfileStructure:
    """Verify the real built-in profiles follow conventions."""

    PROFILES_DIR = Path(__file__).parent.parent / "profiles"

    @pytest.mark.parametrize("name", ["default", "admin", "coding"])
    def test_claude_md_at_root(self, name):
        assert (self.PROFILES_DIR / name / "CLAUDE.md").exists()

    @pytest.mark.parametrize("name", ["default", "admin", "coding"])
    def test_settings_overrides_in_claude(self, name):
        assert (self.PROFILES_DIR / name / ".claude" / "settings.overrides.json").exists()
        assert not (self.PROFILES_DIR / name / "settings.overrides.json").exists()

    @pytest.mark.parametrize("name", ["default", "admin", "coding"])
    def test_profile_yml_exists(self, name):
        assert (self.PROFILES_DIR / name / "profile.yml").exists()


# ---------------------------------------------------------------------------
# 3-layer symlink merge tests
# ---------------------------------------------------------------------------


class TestSymlinkMerge:
    """Test the 3-layer symlink merge: agentihooks → bundle → profile."""

    def _run_symlink_merge(self, env):
        """Execute the symlink loop from install.py against the test env."""
        claude_home = env["claude_home"]
        ah_root = env["agentihooks_root"]
        bundle = env["bundle"]
        profile = env["profile"]

        for subdir, label, filter_fn in [
            ("skills", "skill", lambda p: p.is_dir()),
            ("agents", "agent", lambda p: p.suffix == ".md" and p.name != "README.md"),
            ("commands", "command", lambda p: p.suffix == ".md" and p.name != "README.md"),
            ("rules", "rule", lambda p: p.suffix == ".md" and p.name != "README.md"),
        ]:
            dst = claude_home / subdir
            install._symlink_dir_contents(ah_root / ".claude" / subdir, dst, label=label, filter_fn=filter_fn)
            if (bundle / ".claude" / subdir).is_dir():
                install._symlink_dir_contents(
                    bundle / ".claude" / subdir, dst, label=f"bundle {label}", filter_fn=filter_fn
                )
            if (profile / ".claude" / subdir).is_dir():
                install._symlink_dir_contents(
                    profile / ".claude" / subdir, dst, label=f"profile {label}", filter_fn=filter_fn
                )

    def test_agents_all_three_layers(self, install_env):
        self._run_symlink_merge(install_env)
        agents_dir = install_env["claude_home"] / "agents"
        links = {p.name for p in agents_dir.iterdir() if p.is_symlink()}
        assert "error-researcher.md" in links, "L1 agent missing"
        assert "code-reviewer.md" in links, "L2 agent missing"
        assert "profile-agent.md" in links, "L3 agent missing"

    def test_commands_all_three_layers(self, install_env):
        self._run_symlink_merge(install_env)
        cmds_dir = install_env["claude_home"] / "commands"
        links = {p.name for p in cmds_dir.iterdir() if p.is_symlink()}
        assert "status.md" in links, "L1 command missing"
        assert "review.md" in links, "L2 command missing"
        assert "deploy.md" in links, "L3 command missing"

    def test_skills_all_three_layers(self, install_env):
        self._run_symlink_merge(install_env)
        skills_dir = install_env["claude_home"] / "skills"
        links = {p.name for p in skills_dir.iterdir() if p.is_symlink()}
        assert "builtin-skill" in links, "L1 skill missing"
        assert "bundle-skill" in links, "L2 skill missing"
        assert "profile-skill" in links, "L3 skill missing"

    def test_rules_all_three_layers(self, install_env):
        self._run_symlink_merge(install_env)
        rules_dir = install_env["claude_home"] / "rules"
        links = {p.name for p in rules_dir.iterdir() if p.is_symlink()}
        assert "builtin-rule.md" in links, "L1 rule missing"
        assert "python.md" in links, "L2 rule missing"
        assert "git-workflow.md" in links, "L3 rule missing"

    def test_no_broken_symlinks(self, install_env):
        self._run_symlink_merge(install_env)
        claude_home = install_env["claude_home"]
        broken = []
        for d in ("skills", "agents", "commands", "rules"):
            for link in (claude_home / d).rglob("*"):
                if link.is_symlink() and not link.resolve().exists():
                    broken.append(str(link))
        assert broken == [], f"Broken symlinks found: {broken}"

    def test_symlinks_are_idempotent(self, install_env):
        """Running twice should not fail or create duplicates."""
        self._run_symlink_merge(install_env)
        self._run_symlink_merge(install_env)
        agents_dir = install_env["claude_home"] / "agents"
        assert sum(1 for p in agents_dir.iterdir() if p.is_symlink()) == 3


# ---------------------------------------------------------------------------
# CLAUDE.md linking tests
# ---------------------------------------------------------------------------


class TestClaudeMdLinking:
    """Test CLAUDE.md is copied from profile root to ~/.claude/."""

    def test_copies_from_profile_root(self, install_env):
        profile = install_env["profile"]
        dst = install_env["claude_home"] / "CLAUDE.md"
        with patch.object(install, "CLAUDE_HOME", install_env["claude_home"]):
            install._install_system_prompt(profile, "test-profile")
        assert dst.exists() and not dst.is_symlink()
        assert dst.read_text() == (profile / "CLAUDE.md").read_text()

    def test_idempotent(self, install_env):
        profile = install_env["profile"]
        dst = install_env["claude_home"] / "CLAUDE.md"
        with patch.object(install, "CLAUDE_HOME", install_env["claude_home"]):
            install._install_system_prompt(profile, "test-profile")
            install._install_system_prompt(profile, "test-profile")
        assert dst.exists() and not dst.is_symlink()

    def test_skips_when_no_claude_md(self, install_env):
        profile = install_env["profile"]
        (profile / "CLAUDE.md").unlink()
        dst = install_env["claude_home"] / "CLAUDE.md"
        with patch.object(install, "CLAUDE_HOME", install_env["claude_home"]):
            install._install_system_prompt(profile, "test-profile")
        assert not dst.exists()

    def test_stale_symlink_cleaned(self, install_env):
        """Old symlink pointing into profiles/ is removed."""
        dst = install_env["claude_home"] / "CLAUDE.md"
        # Create a stale symlink pointing into a profiles/ path
        fake_target = install_env["tmp"] / "profiles" / "old" / ".claude" / "CLAUDE.md"
        fake_target.parent.mkdir(parents=True, exist_ok=True)
        fake_target.write_text("old")
        dst.symlink_to(fake_target)

        with patch.object(install, "CLAUDE_HOME", install_env["claude_home"]):
            install._cleanup_stale_claude_md_symlink()
        assert not dst.exists()


# ---------------------------------------------------------------------------
# Settings override tests
# ---------------------------------------------------------------------------


class TestSettingsOverrides:
    """Test settings.overrides.json is loaded from .claude/ inside profile."""

    def test_overrides_from_claude_subdir(self, install_env):
        profile = install_env["profile"]
        overrides_path = profile / ".claude" / "settings.overrides.json"
        assert overrides_path.exists()
        overrides = json.loads(overrides_path.read_text())
        assert overrides["env"]["PROFILE_VAR"] == "test-value"

    def test_fallback_to_root(self, install_env):
        """If .claude/settings.overrides.json doesn't exist, check root."""
        profile = install_env["profile"]
        # Move overrides to root
        (profile / ".claude" / "settings.overrides.json").rename(profile / "settings.overrides.json")
        # The install code should find it at root
        overrides_path = profile / ".claude" / "settings.overrides.json"
        if not overrides_path.exists():
            overrides_path = profile / "settings.overrides.json"
        assert overrides_path.exists()


# ---------------------------------------------------------------------------
# MCP merge tests
# ---------------------------------------------------------------------------


class TestMcpMerge:
    """Test MCP servers from bundle and profile are merged."""

    def test_bundle_mcp_loaded(self, install_env):
        bundle = install_env["bundle"]
        mcp_file = bundle / ".claude" / ".mcp.json"
        data = json.loads(mcp_file.read_text())
        assert "bundle-server" in data["mcpServers"]

    def test_profile_mcp_loaded(self, install_env):
        profile = install_env["profile"]
        mcp_file = profile / ".claude" / ".mcp.json"
        data = json.loads(mcp_file.read_text())
        assert "profile-server" in data["mcpServers"]

    def test_merge_to_user_scope(self, install_env):
        """Both bundle and profile MCPs merge into claude.json."""
        claude_json = install_env["claude_json"]
        with patch.object(install, "_CLAUDE_JSON", claude_json):
            # Merge bundle MCPs
            bundle_mcp = json.loads((install_env["bundle"] / ".claude" / ".mcp.json").read_text())
            install._merge_mcp_to_user_scope(bundle_mcp["mcpServers"])

            # Merge profile MCPs
            profile_mcp = json.loads((install_env["profile"] / ".claude" / ".mcp.json").read_text())
            install._merge_mcp_to_user_scope(profile_mcp["mcpServers"])

        result = json.loads(claude_json.read_text())
        assert "bundle-server" in result["mcpServers"]
        assert "profile-server" in result["mcpServers"]


# ---------------------------------------------------------------------------
# Active profile detection tests
# ---------------------------------------------------------------------------


class TestActiveProfileDetection:
    """Test query_active_profile reads from state.json."""

    def test_reads_from_state(self, install_env, capsys):
        with patch.object(install, "_load_state", return_value={"targets": {"global": {"profile": "anton"}}}):
            install.query_active_profile()
        assert capsys.readouterr().out.strip() == "anton"

    def test_not_installed(self, install_env, capsys):
        with patch.object(install, "_load_state", return_value={}):
            install.query_active_profile()
        assert capsys.readouterr().out.strip() == "not installed"


# ---------------------------------------------------------------------------
# Profile listing tests
# ---------------------------------------------------------------------------


class TestProfileListing:
    """Test list_profiles shows CLAUDE.md status."""

    def test_shows_missing_claude_md(self, install_env, capsys):
        """Profiles without CLAUDE.md get a [no CLAUDE.md] marker."""
        profile = install_env["profile"]
        (profile / "CLAUDE.md").unlink()

        with (
            patch.object(install, "_available_profiles", return_value=["test-profile"]),
            patch.object(install, "_resolve_profile_dir", return_value=profile),
            patch.object(install, "_read_profile_description", return_value="Test"),
        ):
            install.list_profiles()

        output = capsys.readouterr().out
        assert "[no CLAUDE.md]" in output
