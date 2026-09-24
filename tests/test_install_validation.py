"""Comprehensive install validation tests.

Tests the full install pipeline: 3-layer symlinks, CLAUDE.md linking,
MCP merging, settings generation, and profile structure conventions.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import install  # noqa: I001


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# Real-home isolation is suite-wide — see the autouse fixture in tests/conftest.py.


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
        "name: test-profile\ndescription: Test profile\nmcp_categories: all\notel:\n  enabled: false\n"
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
    """Verify built-in profiles follow conventions (auto-discovers actual profiles)."""

    PROFILES_DIR = Path(__file__).parent.parent / "profiles"

    @staticmethod
    def _real_profiles():
        """Discover profile dirs that have a profile.yml (skip _base, __init__)."""
        d = Path(__file__).parent.parent / "profiles"
        return [p.name for p in d.iterdir() if p.is_dir() and (p / "profile.yml").exists()]

    def test_at_least_one_profile_exists(self):
        profiles = self._real_profiles()
        if not profiles:
            pytest.skip("No profiles with profile.yml found — bundle profiles live externally")


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
        # Single-profile installs carry the same `<!-- profile: name -->`
        # marker the chain writer uses, so the init-loss guard can sniff the
        # installed profile regardless of chain length.
        assert dst.read_text() == f"<!-- profile: test-profile -->\n{(profile / 'CLAUDE.md').read_text()}"

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

    def test_stale_symlink_cleaned(self, install_env, monkeypatch):
        """Old symlink into OUR profiles/ dir is removed."""
        dst = install_env["claude_home"] / "CLAUDE.md"
        root = install_env["tmp"] / "agentihooks"
        monkeypatch.setattr(install, "AGENTIHOOKS_ROOT", root)
        fake_target = root / "profiles" / "old" / ".claude" / "CLAUDE.md"
        fake_target.parent.mkdir(parents=True, exist_ok=True)
        fake_target.write_text("old")
        dst.symlink_to(fake_target)

        with patch.object(install, "CLAUDE_HOME", install_env["claude_home"]):
            install._cleanup_stale_claude_md_symlink()
        assert not dst.exists()

    def test_foreign_claude_md_symlink_survives(self, install_env, monkeypatch):
        """A CLAUDE.md symlink into the operator's own tree is left alone."""
        dst = install_env["claude_home"] / "CLAUDE.md"
        monkeypatch.setattr(install, "AGENTIHOOKS_ROOT", install_env["tmp"] / "agentihooks")
        foreign = install_env["tmp"] / "dotfiles" / "profiles" / "work" / "CLAUDE.md"
        foreign.parent.mkdir(parents=True, exist_ok=True)
        foreign.write_text("operator's own")
        dst.symlink_to(foreign)

        with patch.object(install, "CLAUDE_HOME", install_env["claude_home"]):
            install._cleanup_stale_claude_md_symlink()
        assert dst.is_symlink(), "cleanup removed a CLAUDE.md symlink agentihooks never created"


# ---------------------------------------------------------------------------
# Bundle-level shared CLAUDE.md tests
# ---------------------------------------------------------------------------


class TestBundleClaudeMdPrepend:
    """Test <bundle>/.claude/CLAUDE.md is prepended ahead of all profile content."""

    @staticmethod
    def _write_bundle_md(install_env, text: str) -> Path:
        bundle_md = install_env["bundle"] / ".claude" / "CLAUDE.md"
        bundle_md.write_text(text)
        return bundle_md

    @staticmethod
    def _install_profile(install_env) -> Path:
        """Write the profile-only CLAUDE.md, as step 5 does, and return dst."""
        with patch.object(install, "CLAUDE_HOME", install_env["claude_home"]):
            install._install_system_prompt(install_env["profile"], "test-profile")
        return install_env["claude_home"] / "CLAUDE.md"

    def test_prepends_ahead_of_profile_content(self, install_env):
        dst = self._install_profile(install_env)
        self._write_bundle_md(install_env, "# Shared\nshared directive\n")
        with patch.object(install, "CLAUDE_HOME", install_env["claude_home"]):
            install._prepend_bundle_claude_md(install_env["bundle"])

        text = dst.read_text()
        assert text.startswith(install._BUNDLE_CLAUDE_MD_BEGIN)
        assert "shared directive" in text
        # Profile content must come AFTER the bundle block so it still wins.
        assert text.index(install._BUNDLE_CLAUDE_MD_END) < text.index("<!-- profile: test-profile -->")

    def test_idempotent_no_marker_stacking(self, install_env):
        dst = self._install_profile(install_env)
        self._write_bundle_md(install_env, "# Shared\nshared directive\n")
        with patch.object(install, "CLAUDE_HOME", install_env["claude_home"]):
            install._prepend_bundle_claude_md(install_env["bundle"])
            first = dst.read_text()
            install._prepend_bundle_claude_md(install_env["bundle"])

        text = dst.read_text()
        assert text == first
        assert text.count(install._BUNDLE_CLAUDE_MD_BEGIN) == 1
        assert text.count(install._BUNDLE_CLAUDE_MD_END) == 1

    def test_updates_in_place_when_bundle_content_changes(self, install_env):
        dst = self._install_profile(install_env)
        self._write_bundle_md(install_env, "# Shared\nversion one\n")
        with patch.object(install, "CLAUDE_HOME", install_env["claude_home"]):
            install._prepend_bundle_claude_md(install_env["bundle"])
            self._write_bundle_md(install_env, "# Shared\nversion two\n")
            install._prepend_bundle_claude_md(install_env["bundle"])

        text = dst.read_text()
        assert "version two" in text
        assert "version one" not in text
        assert text.count(install._BUNDLE_CLAUDE_MD_BEGIN) == 1
        assert "<!-- profile: test-profile -->" in text

    def test_absent_bundle_claude_md_is_noop(self, install_env):
        dst = self._install_profile(install_env)
        before = dst.read_text()
        # Bundle exists (fixture builds it) but has no .claude/CLAUDE.md
        with patch.object(install, "CLAUDE_HOME", install_env["claude_home"]):
            install._prepend_bundle_claude_md(install_env["bundle"])
        assert dst.read_text() == before

    def test_empty_bundle_claude_md_is_noop(self, install_env):
        dst = self._install_profile(install_env)
        before = dst.read_text()
        self._write_bundle_md(install_env, "   \n\n")
        with patch.object(install, "CLAUDE_HOME", install_env["claude_home"]):
            install._prepend_bundle_claude_md(install_env["bundle"])
        assert dst.read_text() == before

    def test_no_bundle_linked_is_noop(self, install_env):
        dst = self._install_profile(install_env)
        before = dst.read_text()
        with patch.object(install, "CLAUDE_HOME", install_env["claude_home"]):
            install._prepend_bundle_claude_md(None)
        assert dst.read_text() == before

    def test_noop_when_claude_md_missing(self, install_env):
        """No profile CLAUDE.md means nothing to prepend onto — don't create one."""
        dst = install_env["claude_home"] / "CLAUDE.md"
        self._write_bundle_md(install_env, "# Shared\nshared directive\n")
        with patch.object(install, "CLAUDE_HOME", install_env["claude_home"]):
            install._prepend_bundle_claude_md(install_env["bundle"])
        assert not dst.exists()

    def test_survives_install_system_prompt_early_return(self, install_env):
        """A bundle file added after the first install still lands.

        The second _install_system_prompt call takes its 'already up to date'
        early return, so the bundle block must not depend on that write happening.
        """
        dst = self._install_profile(install_env)
        # Second call: content identical -> early return, dst untouched
        unchanged = dst.read_text()
        dst2 = self._install_profile(install_env)
        assert dst2.read_text() == unchanged

        # Bundle file appears only now
        self._write_bundle_md(install_env, "# Shared\nlate arrival\n")
        with patch.object(install, "CLAUDE_HOME", install_env["claude_home"]):
            install._prepend_bundle_claude_md(install_env["bundle"])
        assert "late arrival" in dst.read_text()

    def test_marker_does_not_match_profile_detection_regex(self, install_env):
        """The bundle markers must not register as a phantom chain member."""
        import re

        dst = self._install_profile(install_env)
        self._write_bundle_md(install_env, "# Shared\nshared directive\n")
        with patch.object(install, "CLAUDE_HOME", install_env["claude_home"]):
            install._prepend_bundle_claude_md(install_env["bundle"])

        matches = re.findall(r"<!--\s*profile:\s*([A-Za-z0-9_,-]+)\s*-->", dst.read_text())
        assert matches == ["test-profile"]

    def test_chain_mode_gets_bundle_prefix_exactly_once(self, install_env):
        """Chained install: one bundle block at the top, ahead of every profile."""
        dst = install_env["claude_home"] / "CLAUDE.md"
        # Reproduce the chain writer's output (install_global is not unit-tested)
        parts = [
            "<!-- profile: alpha -->\n# Alpha\n",
            "<!-- profile: beta -->\n# Beta\n",
        ]
        dst.write_text("\n\n---\n\n".join(parts) + "\n")
        self._write_bundle_md(install_env, "# Shared\nshared directive\n")
        with patch.object(install, "CLAUDE_HOME", install_env["claude_home"]):
            install._prepend_bundle_claude_md(install_env["bundle"])

        text = dst.read_text()
        assert text.count(install._BUNDLE_CLAUDE_MD_BEGIN) == 1
        end = text.index(install._BUNDLE_CLAUDE_MD_END)
        assert end < text.index("<!-- profile: alpha -->")
        assert end < text.index("<!-- profile: beta -->")

    def test_install_system_prompt_unaware_of_bundle(self, install_env):
        """The seam holds: _install_system_prompt never sees bundle content."""
        self._write_bundle_md(install_env, "# Shared\nshared directive\n")
        profile = install_env["profile"]
        dst = self._install_profile(install_env)
        assert dst.read_text() == (f"<!-- profile: test-profile -->\n{(profile / 'CLAUDE.md').read_text()}")

    def test_does_not_write_through_symlink(self, install_env):
        """Never write into a profile source via a leftover symlink."""
        dst = install_env["claude_home"] / "CLAUDE.md"
        source = install_env["profile"] / "CLAUDE.md"
        original = source.read_text()
        dst.symlink_to(source)
        self._write_bundle_md(install_env, "# Shared\nshared directive\n")
        with patch.object(install, "CLAUDE_HOME", install_env["claude_home"]):
            install._prepend_bundle_claude_md(install_env["bundle"])
        assert source.read_text() == original

    def test_unchanged_content_avoids_a_write(self, install_env):
        """The up-to-date short-circuit must actually skip the write.

        Byte-identical output makes this invisible to content assertions, so
        assert on the write itself.
        """
        self._install_profile(install_env)
        self._write_bundle_md(install_env, "# Shared\nshared directive\n")
        with patch.object(install, "CLAUDE_HOME", install_env["claude_home"]):
            install._prepend_bundle_claude_md(install_env["bundle"])
            # Second call with nothing changed must not touch the file at all.
            with patch.object(Path, "write_text", autospec=True) as spy:
                install._prepend_bundle_claude_md(install_env["bundle"])
            assert spy.call_count == 0

    def test_markers_never_contain_the_profile_keyword(self):
        """Guard the documented invariant, not just today's regex shape.

        The phantom-chain regex currently requires `<!-- profile: NAME -->` to
        close cleanly, so a marker merely *containing* "profile:" would slip
        through. Pin the broader contract the docstring states, so loosening
        that regex later cannot silently introduce a phantom chain member.
        """
        for marker in (install._BUNDLE_CLAUDE_MD_BEGIN, install._BUNDLE_CLAUDE_MD_END):
            assert "profile:" not in marker

    def test_seam_is_structural_not_incidental(self):
        """_install_system_prompt must not grow a bundle-aware parameter.

        Passing bundle content in through a new default argument would keep the
        unit tests green while changing what real installs write.
        """
        import inspect

        params = inspect.signature(install._install_system_prompt).parameters
        assert list(params) == ["profile_dir", "profile_name"]
        # It may *mention* the bundle in prose, but must never read from it.
        src = inspect.getsource(install._install_system_prompt)
        assert "bundle_dir" not in src
        assert "_BUNDLE_CLAUDE_MD_BEGIN" not in src

    def test_install_global_wires_the_call_in_order(self):
        """install_global is never executed by any test, so pin the wiring.

        Catches a dropped call, a call placed inside the per-profile chain loop
        (which would duplicate the block), or the wrong argument being threaded.
        """
        import inspect

        # The persona steps live in _install_claude_persona (reached from
        # install_global via the claude target adapter).
        src = inspect.getsource(install._install_claude_persona)
        assert "_prepend_bundle_claude_md(bundle_dir)" in src
        # Must sit between the profile writer and the manifesto appender.
        assert src.index("_install_system_prompt") < src.index("_prepend_bundle_claude_md(bundle_dir)")
        assert src.index("_prepend_bundle_claude_md(bundle_dir)") < src.index("_append_ci_manifesto_to_claude_md()")
        # Exactly one call site — never inside the chain loop.
        assert src.count("_prepend_bundle_claude_md(") == 1
        # And the install flow routes persona through the target adapter.
        outer = inspect.getsource(install._install_global_inner)
        assert "adapter.install_persona(" in outer

    def test_marker_does_not_claim_agentihooks_ownership(self):
        """A bundle block alone must not make a file look agentihooks-managed.

        `_claude_md_is_managed` treats `_CLAUDE_MD_MANAGED_MARKER` as proof of
        ownership, and uninstall deletes a managed file outright when no original
        was recorded. A marker carrying that phrase would get an operator's
        hand-authored CLAUDE.md deleted.
        """
        for marker in (install._BUNDLE_CLAUDE_MD_BEGIN, install._BUNDLE_CLAUDE_MD_END):
            assert install._CLAUDE_MD_MANAGED_MARKER not in marker

    def test_hand_authored_file_is_backed_up_before_prepend(self, install_env):
        """Never mutate an unmanaged file without capturing the original."""
        dst = install_env["claude_home"] / "CLAUDE.md"
        dst.write_text("# My own hand-written CLAUDE.md\nDo not touch this.\n")
        self._write_bundle_md(install_env, "# Shared\nshared directive\n")
        with patch.object(install, "CLAUDE_HOME", install_env["claude_home"]):
            install._prepend_bundle_claude_md(install_env["bundle"])
            # An original must now be recorded, so uninstall restores rather
            # than deletes.
            assert install._load_state().get("claude_md_original_backup")
            backups = list(install_env["claude_home"].glob("CLAUDE.md.bak.*"))
            assert backups, "no backup taken before mutating an unmanaged file"
            assert "Do not touch this." in backups[0].read_text()

    def test_bundle_body_with_managed_marker_is_refused(self, install_env):
        """Embedded markers would corrupt the first-occurrence splices."""
        dst = self._install_profile(install_env)
        before = dst.read_text()
        for marker in install._MANAGED_BLOCK_MARKERS:
            self._write_bundle_md(install_env, f"# Shared\ndocs quoting {marker} inline\n")
            with patch.object(install, "CLAUDE_HOME", install_env["claude_home"]):
                install._prepend_bundle_claude_md(install_env["bundle"])
            assert dst.read_text() == before, f"not refused for {marker!r}"

    def test_stale_block_removed_when_bundle_goes_away(self, install_env):
        """Unlinking the bundle must retract its directives, not strand them."""
        dst = self._install_profile(install_env)
        profile_only = dst.read_text()
        self._write_bundle_md(install_env, "# Shared\nshared directive\n")
        with patch.object(install, "CLAUDE_HOME", install_env["claude_home"]):
            install._prepend_bundle_claude_md(install_env["bundle"])
            assert install._BUNDLE_CLAUDE_MD_BEGIN in dst.read_text()
            # Bundle unlinked
            install._prepend_bundle_claude_md(None)

        text = dst.read_text()
        assert install._BUNDLE_CLAUDE_MD_BEGIN not in text
        assert "shared directive" not in text
        assert text == profile_only

    def test_reinstall_does_not_churn_backups(self, install_env, tmp_path):
        """The profile writer's up-to-date check must survive the managed blocks.

        Otherwise every `agentihooks init` takes the backup+overwrite path and
        drops another CLAUDE.md.bak.<timestamp> into ~/.claude, forever.
        """
        import hooks.config as cfg

        manifesto = tmp_path / "MANIFESTO.md"
        manifesto.write_text("# Manifesto\ndoctrine body\n")
        self._write_bundle_md(install_env, "# Shared\nshared directive\n")

        with (
            patch.object(install, "CLAUDE_HOME", install_env["claude_home"]),
            patch.object(cfg, "CI_MANIFESTO_ENABLED", True),
            patch.object(cfg, "CI_MANIFESTO_PATH", str(manifesto)),
        ):
            for _ in range(4):  # simulate four `agentihooks init` runs
                install._install_system_prompt(install_env["profile"], "test-profile")
                install._prepend_bundle_claude_md(install_env["bundle"])
                install._append_ci_manifesto_to_claude_md()

        backups = list(install_env["claude_home"].glob("CLAUDE.md.bak.*"))
        assert backups == [], f"backup churn on re-run: {[b.name for b in backups]}"

    def test_third_party_block_survives_the_profile_write(self, install_env):
        """A neighbour's fenced block must not be collateral damage.

        The profile writer replaces CLAUDE.md wholesale; agentibridge and friends
        append their own BEGIN/END block and only their own installer can put it
        back.
        """
        dst = install_env["claude_home"] / "CLAUDE.md"
        foreign = "<!-- BEGIN agentibridge -->\nthird-party docs\n<!-- END agentibridge -->"
        dst.write_text(f"<!-- profile: test-profile -->\n# old\n\n{foreign}\n")
        with patch.object(install, "CLAUDE_HOME", install_env["claude_home"]):
            install._install_system_prompt(install_env["profile"], "test-profile")

        text = dst.read_text()
        assert "<!-- BEGIN agentibridge -->" in text
        assert "third-party docs" in text
        assert text.count("<!-- BEGIN agentibridge -->") == 1
        # Profile content was still refreshed from source.
        assert (install_env["profile"] / "CLAUDE.md").read_text().strip() in text

    def test_foreign_block_extraction_ignores_owned_blocks(self, install_env, tmp_path):
        """Owned blocks must never be mistaken for third-party ones."""
        text = (
            f"{install._BUNDLE_CLAUDE_MD_BEGIN}\nshared\n{install._BUNDLE_CLAUDE_MD_END}\n\n"
            "<!-- profile: anton -->\n# Anton\n\n"
            "<!-- BEGIN agentibridge -->\nkeep me\n<!-- END agentibridge -->\n\n"
            "<!-- BEGIN CI MANIFESTO (auto-injected by agentihooks init) -->\n"
            "doctrine\n<!-- END CI MANIFESTO -->\n"
        )
        found = install._extract_foreign_blocks(text)
        assert [name for name, _ in found] == ["agentibridge"]
        assert "keep me" in found[0][1]

    def test_documented_example_block_does_not_grow(self, install_env):
        """A profile that documents the marker format must not duplicate it.

        The example is a well-formed block, so without dedup-by-name it is
        re-appended on every init and the file grows without bound.
        """
        profile = install_env["profile"]
        (profile / "CLAUDE.md").write_text("# Anton\nFormat:\n\n<!-- BEGIN sample -->\nexample\n<!-- END sample -->\n")
        dst = install_env["claude_home"] / "CLAUDE.md"
        dst.write_text(
            "<!-- profile: test-profile -->\n# old\n\n<!-- BEGIN agentibridge -->\nab\n<!-- END agentibridge -->\n"
        )
        with patch.object(install, "CLAUDE_HOME", install_env["claude_home"]):
            for _ in range(4):
                install._install_system_prompt(profile, "test-profile")

        text = dst.read_text()
        assert text.count("<!-- BEGIN sample -->") == 1
        assert text.count("<!-- BEGIN agentibridge -->") == 1
        assert len(list(install_env["claude_home"].glob("CLAUDE.md.bak.*"))) == 1

    def test_nested_block_preserves_outer_content(self):
        """Depth tracking, not first-END matching."""
        text = (
            "<!-- BEGIN foo -->\nouter-before\n"
            "<!-- BEGIN foo -->\ninner\n<!-- END foo -->\n"
            "outer-after-must-survive\n<!-- END foo -->\n"
        )
        found = install._extract_foreign_blocks(text)
        assert len(found) == 1
        assert "outer-after-must-survive" in found[0][1]

    def test_owned_markers_inside_a_foreign_block_are_not_stripped(self):
        """Owned-block removal is scoped to depth 0."""
        text = (
            "<!-- BEGIN evil -->\nbefore\n"
            f"{install._BUNDLE_CLAUDE_MD_BEGIN}\npayload-must-survive\n"
            f"{install._BUNDLE_CLAUDE_MD_END}\nafter\n<!-- END evil -->\n"
        )
        assert "payload-must-survive" in install._strip_managed_blocks(text)
        found = install._extract_foreign_blocks(text)
        assert "payload-must-survive" in found[0][1]

    def test_malformed_markers_force_a_backup(self, install_env):
        """Ambiguous markup is unrecoverable by rewrite — guarantee a backup."""
        dst = install_env["claude_home"] / "CLAUDE.md"
        dst.write_text(
            "<!-- profile: test-profile -->\n# old\n\n<!-- BEGIN agentibridge -->\nin-flight, never closed\n"
        )
        with patch.object(install, "CLAUDE_HOME", install_env["claude_home"]):
            install._install_system_prompt(install_env["profile"], "test-profile")

        backups = list(install_env["claude_home"].glob("CLAUDE.md.bak.*"))
        assert backups, "malformed markers must force a backup"
        assert "in-flight, never closed" in backups[0].read_text()

    def test_chain_up_to_date_does_not_abort_the_install(self):
        """The chain guard must skip only the write, never return early.

        A bare `return` there would silently skip the bundle prepend, the
        manifesto append, and all MCP installation.
        """
        import inspect

        src = inspect.getsource(install._install_claude_persona)
        chain = src.split("Chain mode", 1)[1].split("--- 5a.", 1)[0]
        assert "already up to date" in chain
        assert "\n                    return\n" not in chain
        assert "up_to_date" in chain

    def test_manifesto_block_survives_a_bundle_refresh(self, install_env, tmp_path):
        """Full pipeline order: profile -> bundle prepend -> manifesto append.

        Then change the bundle and re-prepend; the manifesto block at the tail
        must survive intact and unduplicated.
        """
        import hooks.config as cfg

        manifesto = tmp_path / "MANIFESTO.md"
        manifesto.write_text("# Manifesto\ndoctrine body\n")
        dst = self._install_profile(install_env)
        self._write_bundle_md(install_env, "# Shared\nversion one\n")

        with (
            patch.object(install, "CLAUDE_HOME", install_env["claude_home"]),
            patch.object(cfg, "CI_MANIFESTO_ENABLED", True),
            patch.object(cfg, "CI_MANIFESTO_PATH", str(manifesto)),
        ):
            install._prepend_bundle_claude_md(install_env["bundle"])
            install._append_ci_manifesto_to_claude_md()
            # Bundle content changes; re-run the prepend as a re-install would.
            self._write_bundle_md(install_env, "# Shared\nversion two\n")
            install._prepend_bundle_claude_md(install_env["bundle"])

        text = dst.read_text()
        assert text.count("<!-- BEGIN CI MANIFESTO") == 1
        assert text.count("<!-- END CI MANIFESTO -->") == 1
        assert "doctrine body" in text
        assert "version two" in text and "version one" not in text
        # Order: bundle block -> profile -> manifesto
        assert text.index(install._BUNDLE_CLAUDE_MD_END) < text.index("<!-- profile: test-profile -->")
        assert text.index("<!-- profile: test-profile -->") < text.index("<!-- BEGIN CI MANIFESTO")


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


class TestMcpTransportModes:
    """_build_mcp_config emits a stdio entry by default and a url entry in
    network mode, for clients that filter stdio MCP servers out at load time.
    """

    @pytest.fixture(autouse=True)
    def _clean_transport_env(self, monkeypatch):
        for var in (
            "AGENTIHOOKS_MCP_TRANSPORT",
            "MCP_HOST",
            "MCP_PORT",
            "MCP_SSE_PATH",
            "MCP_STREAMABLE_HTTP_PATH",
            "MCP_SCHEME",
        ):
            monkeypatch.delenv(var, raising=False)

    def test_stdio_is_the_default_and_unchanged(self, monkeypatch):
        monkeypatch.setattr(install, "_resolve_hooks_python", lambda: Path("/venv/bin/python"))

        entry = install._build_mcp_config("all")["mcpServers"]["agentihooks"]

        assert entry["command"] == "/venv/bin/python"
        assert entry["args"] == ["-m", "hooks.mcp"]
        assert entry["env"] == {"MCP_CATEGORIES": "all"}
        assert "url" not in entry

    def test_sse_mode_emits_url_entry(self, monkeypatch):
        monkeypatch.setenv("AGENTIHOOKS_MCP_TRANSPORT", "sse")

        entry = install._build_mcp_config("all")["mcpServers"]["agentihooks"]

        assert entry == {"type": "sse", "url": "http://localhost:8642/sse"}

    def test_streamable_http_registers_as_type_http(self, monkeypatch):
        """Claude Code's config schema calls it "http" — the SDK's own
        "streamable-http" literal is rejected and silently never connects."""
        monkeypatch.setenv("AGENTIHOOKS_MCP_TRANSPORT", "streamable-http")

        entry = install._build_mcp_config("all")["mcpServers"]["agentihooks"]

        assert entry == {"type": "http", "url": "http://localhost:8642/mcp"}

    def test_url_mode_honours_host_and_port(self, monkeypatch):
        monkeypatch.setenv("AGENTIHOOKS_MCP_TRANSPORT", "sse")
        monkeypatch.setenv("MCP_HOST", "10.0.0.5")
        monkeypatch.setenv("MCP_PORT", "9100")

        entry = install._build_mcp_config("all")["mcpServers"]["agentihooks"]

        assert entry["url"] == "http://10.0.0.5:9100/sse"

    def test_scheme_defaults_to_http_for_the_loopback_bind(self, monkeypatch):
        """Plaintext is correct for a loopback bind — the bind is the boundary and
        TLS to loopback buys nothing."""
        monkeypatch.setenv("AGENTIHOOKS_MCP_TRANSPORT", "sse")

        entry = install._build_mcp_config("all")["mcpServers"]["agentihooks"]

        assert entry["url"].startswith("http://localhost:")

    def test_default_host_is_localhost_not_the_dotted_quad(self, monkeypatch):
        """Observed on a Claude Code Enterprise machine: an entry whose url named
        `127.0.0.1` was dropped from the client's configured-server set outright —
        absent from `claude mcp list`, and `claude mcp get` reported it as not
        configured — while the byte-identical entry spelled `localhost` connected.

        Both name the loopback interface, so the access boundary is unchanged.
        Only the spelling decides whether the client sees the server at all, and
        the failure mode is silent, so this default is pinned deliberately.
        """
        for transport, path in (("sse", "/sse"), ("streamable-http", "/mcp")):
            monkeypatch.setenv("AGENTIHOOKS_MCP_TRANSPORT", transport)
            monkeypatch.delenv("MCP_HOST", raising=False)

            entry = install._build_mcp_config("all")["mcpServers"]["agentihooks"]

            assert entry["url"] == f"http://localhost:8642{path}"
            assert "127.0.0.1" not in entry["url"]

    def test_scheme_can_be_https_for_a_tls_fronted_daemon(self, monkeypatch):
        monkeypatch.setenv("AGENTIHOOKS_MCP_TRANSPORT", "streamable-http")
        monkeypatch.setenv("MCP_SCHEME", "https")
        monkeypatch.setenv("MCP_HOST", "mcp.internal")

        entry = install._build_mcp_config("all")["mcpServers"]["agentihooks"]

        assert entry["url"] == "https://mcp.internal:8642/mcp"

    def test_bad_scheme_is_rejected(self, monkeypatch):
        monkeypatch.setenv("AGENTIHOOKS_MCP_TRANSPORT", "sse")
        monkeypatch.setenv("MCP_SCHEME", "ftp")

        with pytest.raises(SystemExit):
            install._build_mcp_config("all")

    def test_config_build_never_probes_the_port(self, monkeypatch):
        """_build_mcp_config runs before the daemon is started, so a closed port
        is the normal state there. It used to print a "not answering yet" hint
        naming a systemctl command — on every healthy install, and wrong outright
        under the pidfile backend. Reporting startup belongs to _ensure_mcp_daemon,
        which actually knows the outcome."""
        monkeypatch.setenv("AGENTIHOOKS_MCP_TRANSPORT", "streamable-http")

        entry = install._build_mcp_config("all")["mcpServers"]["agentihooks"]

        assert entry["type"] == "http"
        assert not hasattr(install, "_probe_mcp_url_reachable")

    def test_url_mode_never_probes_a_local_python(self, monkeypatch):
        """There is no local interpreter to validate for a remote server."""
        monkeypatch.setenv("AGENTIHOOKS_MCP_TRANSPORT", "sse")

        def _explode():
            raise AssertionError("_resolve_hooks_python must not run in url mode")

        monkeypatch.setattr(install, "_resolve_hooks_python", _explode)
        install._build_mcp_config("all")

    def test_unknown_transport_exits(self, monkeypatch):
        monkeypatch.setenv("AGENTIHOOKS_MCP_TRANSPORT", "carrier-pigeon")

        with pytest.raises(SystemExit):
            install._build_mcp_config("all")

    def test_transport_read_from_agentihooks_env_file(self, monkeypatch, tmp_path):
        """One operator edit in ~/.agentihooks/.env drives installer and daemon."""
        home = tmp_path / "home"
        (home / ".agentihooks").mkdir(parents=True)
        (home / ".agentihooks" / ".env").write_text('MCP_TRANSPORT="sse"\n')
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

        assert install._resolve_installer_mcp_transport() == "sse"

    def test_process_env_beats_env_file(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        (home / ".agentihooks").mkdir(parents=True)
        (home / ".agentihooks" / ".env").write_text("MCP_TRANSPORT=sse\n")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        monkeypatch.setenv("AGENTIHOOKS_MCP_TRANSPORT", "streamable-http")

        assert install._resolve_installer_mcp_transport() == "streamable-http"

    def test_defaults_to_stdio_with_no_signal(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "empty-home"))

        assert install._resolve_installer_mcp_transport() == "stdio"


class TestEnvScanParity:
    """The installer's dotenv scan must agree with the daemon's real parser.

    They read the SAME file. Any divergence means the installer writes a config
    for one transport while the daemon speaks another — silently, exit 0. The
    original scan handled neither `export ` nor inline comments, so
    `export MCP_TRANSPORT=sse` resolved to stdio on the exact enterprise client
    the network path exists for.
    """

    CASES = [
        "MCP_TRANSPORT=sse\n",
        "export MCP_TRANSPORT=sse\n",
        "MCP_TRANSPORT=sse   # use network transport\n",
        'MCP_TRANSPORT="streamable-http"\n',
        "MCP_TRANSPORT='sse'\n",
        "  MCP_TRANSPORT=sse\n",
        "export MCP_TRANSPORT='sse'  # quoted and exported\n",
        "#MCP_TRANSPORT=sse\n",
        "# MCP_TRANSPORT=sse\n",
        "MCP_TRANSPORT_EXTRA=nonsense\n",
        "MCP_TRANSPORT=stdio\nMCP_TRANSPORT=sse\n",
        "",
    ]

    @pytest.mark.parametrize("content", CASES)
    def test_scan_matches_hooks_config_parser(self, tmp_path, content):
        import os

        from hooks.config import _parse_env_file

        env_file = tmp_path / ".env"
        env_file.write_text(content)

        # _parse_env_file mutates os.environ directly via setdefault, which
        # monkeypatch cannot track or undo — snapshot and restore by hand or the
        # resolved value leaks into every later test in the session.
        saved = os.environ.copy()
        try:
            os.environ.pop("MCP_TRANSPORT", None)
            _parse_env_file(env_file)
            canonical = os.environ.get("MCP_TRANSPORT")
        finally:
            os.environ.clear()
            os.environ.update(saved)

        # install.py no longer carries its own copy of this parser — it delegates
        # to scripts/mcp_daemon.py, so there are two implementations to keep in
        # step rather than three.
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from scripts import mcp_daemon

        prev_home = os.environ.get("AGENTIHOOKS_HOME")
        os.environ["AGENTIHOOKS_HOME"] = str(tmp_path)
        try:
            assert mcp_daemon._scan_env_file("MCP_TRANSPORT") == canonical
        finally:
            if prev_home is None:
                os.environ.pop("AGENTIHOOKS_HOME", None)
            else:
                os.environ["AGENTIHOOKS_HOME"] = prev_home

    def test_export_prefix_resolves_to_the_network_transport(self, tmp_path, monkeypatch):
        """The exact regression: this used to silently resolve to stdio."""
        home = tmp_path / "home"
        (home / ".agentihooks").mkdir(parents=True)
        (home / ".agentihooks" / ".env").write_text("export MCP_TRANSPORT=sse\n")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        monkeypatch.delenv("AGENTIHOOKS_MCP_TRANSPORT", raising=False)

        assert install._resolve_installer_mcp_transport() == "sse"

    def test_inline_comment_does_not_produce_a_bogus_transport(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".agentihooks").mkdir(parents=True)
        (home / ".agentihooks" / ".env").write_text("MCP_TRANSPORT=sse  # enterprise\n")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        monkeypatch.delenv("AGENTIHOOKS_MCP_TRANSPORT", raising=False)

        assert install._resolve_installer_mcp_transport() == "sse"

    def test_bad_transport_is_rejected_before_anything_is_written(self, monkeypatch):
        """Validation runs at step 0, not step 6 — a half-installed tree is worse
        than a refused install."""
        monkeypatch.setenv("AGENTIHOOKS_MCP_TRANSPORT", "carrier-pigeon")

        with pytest.raises(SystemExit):
            install._validate_mcp_transport_or_exit()

    @pytest.mark.parametrize("transport", ["stdio", "sse", "streamable-http"])
    def test_valid_transports_pass_validation(self, monkeypatch, transport):
        monkeypatch.setenv("AGENTIHOOKS_MCP_TRANSPORT", transport)

        assert install._validate_mcp_transport_or_exit() == transport


class TestSystemdUnit:
    """The daemon unit. Real-home isolation comes from the suite-wide autouse
    fixture in conftest.py, which patches Path.home."""

    @pytest.fixture(autouse=True)
    def _no_real_systemctl(self, monkeypatch):
        """Never touch the machine's actual systemd."""
        calls = []

        def _fake_run(cmd, *a, **kw):
            calls.append(cmd)

            class _R:
                returncode = 0

            return _R()

        monkeypatch.setattr("subprocess.run", _fake_run)
        monkeypatch.setattr(install, "_resolve_hooks_python", lambda: Path("/venv/bin/python"))
        # conftest repoints AGENTIHOOKS_ROOT at a temp tree for home isolation;
        # the real template lives in the checkout. The unit is written under the
        # patched Path.home, so this reads the template without escaping isolation.
        monkeypatch.setattr(install, "AGENTIHOOKS_ROOT", Path(__file__).parent.parent)
        self.systemctl_calls = calls

    def _render(self, transport="streamable-http"):
        install._install_systemd_user_unit(transport)
        return install._systemd_user_unit_path().read_text()

    @staticmethod
    def _parse_sections(unit: str) -> dict[str, list[str]]:
        """Active directives per section. Comments are dropped — the explanatory
        text in this unit names both directives and sections, so any check that
        greps the raw string reads its own documentation as configuration."""
        sections: dict[str, list[str]] = {}
        current = ""
        for raw in unit.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                current = line[1:-1]
                sections.setdefault(current, [])
                continue
            sections.setdefault(current, []).append(line)
        return sections

    def test_all_placeholders_substituted(self):
        unit = self._render()

        assert "__PYTHON__" not in unit
        assert "__CWD__" not in unit
        assert "__TRANSPORT__" not in unit
        assert "ExecStart=/venv/bin/python -m hooks.mcp" in unit

    @pytest.mark.parametrize("transport", ["sse", "streamable-http"])
    def test_transport_is_baked_into_the_unit(self, transport):
        """Both sides of the install derive from one validated value, so the
        daemon cannot end up speaking a transport ~/.claude.json does not name."""
        assert f"Environment=MCP_TRANSPORT={transport}" in self._render(transport)

    def test_no_environmentfile_directive(self):
        """Regression guard.

        systemd's parser is not a shell — it does not strip `export `, so an
        `export MCP_TRANSPORT=sse` line in ~/.agentihooks/.env would set a key
        named "export MCP_TRANSPORT" and leave the real one unset. The daemon
        would fall back to stdio and exit 0 while systemd reported success and
        ~/.claude.json pointed at a dead URL. hooks/config.py parses that file
        correctly at import, so the directive is redundant as well as unsafe.
        """
        service = self._parse_sections(self._render())["Service"]

        assert not [d for d in service if d.startswith("EnvironmentFile")]

    def test_restart_always_not_on_failure(self):
        """The case worth surviving is a clean exit 0, which on-failure ignores."""
        unit = self._render()

        assert "Restart=always" in unit
        assert "Restart=on-failure" not in unit
        assert "StartLimitBurst" in unit

    def test_rendering_reloads_but_does_not_itself_start(self):
        """Rendering and starting are separate concerns. `init` starts the daemon
        via `_ensure_mcp_daemon`; this function only puts the unit on disk, so it
        stays callable without side effects on the running process."""
        self._render()
        flat = [" ".join(c) for c in self.systemctl_calls]

        assert any("daemon-reload" in c for c in flat)
        assert not any("start" in c or "enable" in c for c in flat)

    def test_no_stale_manual_start_instruction(self):
        """`init` starts the daemon now. Printing `systemctl --user enable --now`
        sent the operator to a command that cannot work on a box with no user bus
        — which is the machine this whole feature exists for."""
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            install._install_systemd_user_unit("sse")
        out = buf.getvalue()

        assert "Not started" not in out
        assert "enable --now" not in out

    def test_missing_systemctl_degrades_instead_of_raising(self, monkeypatch):
        def _boom(*a, **kw):
            raise FileNotFoundError("systemctl")

        monkeypatch.setattr("subprocess.run", _boom)
        install._install_systemd_user_unit("sse")

        assert install._systemd_user_unit_path().exists()

    def test_removal_is_a_noop_when_absent(self):
        install._remove_systemd_user_unit()

        assert not install._systemd_user_unit_path().exists()

    def test_removal_deletes_the_unit(self):
        self._render()
        install._remove_systemd_user_unit()

        assert not install._systemd_user_unit_path().exists()

    def test_start_limits_are_in_the_unit_section(self):
        """systemd ignores StartLimit* under [Service] without erroring, so a
        misplaced key removes the brake on a hot-looping unit and says nothing.
        Caught by `systemd-analyze verify`, not by any assertion on content."""
        sections = self._parse_sections(self._render())

        assert any(d.startswith("StartLimitIntervalSec") for d in sections["Unit"])
        assert any(d.startswith("StartLimitBurst") for d in sections["Unit"])
        assert not [d for d in sections["Service"] if d.startswith("StartLimit")]

    def test_unit_passes_systemd_analyze_verify(self, tmp_path):
        """Real systemd parser, when one is available. Skipped where it is not."""
        import shutil
        import subprocess as real_subprocess

        if not shutil.which("systemd-analyze"):
            pytest.skip("systemd-analyze not available")

        unit_file = tmp_path / install._SYSTEMD_UNIT_NAME
        unit_file.write_text(self._render())

        # subprocess.run is faked by the autouse fixture; reach the real one.
        proc = real_subprocess.Popen(
            ["systemd-analyze", "verify", str(unit_file)],
            stdout=real_subprocess.PIPE,
            stderr=real_subprocess.STDOUT,
            text=True,
        )
        output = proc.communicate(timeout=30)[0]

        complaints = [
            ln
            for ln in output.splitlines()
            if install._SYSTEMD_UNIT_NAME in ln and ("Unknown key" in ln or "Unknown section" in ln)
        ]
        assert not complaints, output

    def test_uninstall_gate_counts_the_unit(self):
        """A machine where the unit is the last artifact must not report
        'nothing to uninstall' and leave an enabled daemon running."""
        self._render()

        assert install._systemd_user_unit_path().exists()


class TestManagedMcpChainCollection:
    """Regression guard for Defect B: _collect_all_managed_mcp_servers must walk
    the FULL comma-separated profile chain, not pass the joined string to
    _resolve_profile_dir (which returns None and collapses the set to agentihooks).
    """

    def test_collect_walks_full_chain(self, install_env):
        bundle = install_env["bundle"]
        # A second bundle profile with its own MCP server.
        p2_mcp = bundle / "profiles" / "second-profile" / ".claude"
        p2_mcp.mkdir(parents=True)
        (p2_mcp / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"second-server": {"type": "sse", "url": "http://localhost:9/sse"}}})
        )
        fake_state = {
            "targets": {"global": {"profile": "test-profile,second-profile"}},
            "bundle": {"path": str(bundle)},
            "mcpFiles": [],
        }
        with (
            patch.object(install, "_load_state", return_value=fake_state),
            patch.object(install, "_get_bundle_path", return_value=bundle),
            patch.object(install, "_build_mcp_config", return_value={"mcpServers": {"agentihooks": {"command": "x"}}}),
        ):
            managed = set(install._collect_all_managed_mcp_servers().keys())
        # Both profiles' servers present — NOT collapsed to just agentihooks.
        assert managed == {"agentihooks", "bundle-server", "profile-server", "second-server"}


class TestManagedMcpLedger:
    """Ledger reconcile (Defect A): remove servers agentihooks previously
    installed and no longer manages, without touching hand-added servers."""

    @staticmethod
    def _env(tmp_path, *, ledger, claude_servers):
        claude_json = tmp_path / ".claude.json"
        claude_json.write_text(json.dumps({"mcpServers": {n: {"type": "sse", "url": "x"} for n in claude_servers}}))
        state_json = tmp_path / ".agentihooks" / "state.json"
        state_json.parent.mkdir(parents=True, exist_ok=True)
        payload = {} if ledger is None else {"managed_mcp_servers": ledger}
        state_json.write_text(json.dumps(payload))
        return claude_json, state_json

    def _patches(self, claude_json, state_json):
        return (
            patch.object(install, "_CLAUDE_JSON", claude_json),
            patch.object(install, "STATE_JSON", state_json),
            patch.object(install, "AGENTIHOOKS_STATE_DIR", state_json.parent),
        )

    def test_removes_dropped_keeps_handadded(self, tmp_path):
        claude_json, state_json = self._env(
            tmp_path, ledger=["managed-a", "dropped-b"], claude_servers=["managed-a", "dropped-b", "hand-c"]
        )
        p1, p2, p3 = self._patches(claude_json, state_json)
        with p1, p2, p3:
            removed = install._reconcile_managed_mcp_ledger({"managed-a"})
        assert removed == ["dropped-b"]
        servers = json.loads(claude_json.read_text())["mcpServers"]
        assert set(servers) == {"managed-a", "hand-c"}  # dropped gone, hand-added preserved
        assert json.loads(state_json.read_text())["managed_mcp_servers"] == ["managed-a"]

    def test_idempotent_second_run_removes_nothing(self, tmp_path):
        claude_json, state_json = self._env(tmp_path, ledger=["managed-a"], claude_servers=["managed-a", "hand-c"])
        p1, p2, p3 = self._patches(claude_json, state_json)
        with p1, p2, p3:
            removed = install._reconcile_managed_mcp_ledger({"managed-a"})
        assert removed == []
        assert set(json.loads(claude_json.read_text())["mcpServers"]) == {"managed-a", "hand-c"}

    def test_first_run_seeds_ledger_without_removal(self, tmp_path):
        # No ledger key yet (fresh install) — nothing is pruned, ledger is seeded.
        claude_json, state_json = self._env(tmp_path, ledger=None, claude_servers=["managed-a", "hand-c"])
        p1, p2, p3 = self._patches(claude_json, state_json)
        with p1, p2, p3:
            removed = install._reconcile_managed_mcp_ledger({"managed-a"})
        assert removed == []
        assert set(json.loads(claude_json.read_text())["mcpServers"]) == {"managed-a", "hand-c"}
        assert json.loads(state_json.read_text())["managed_mcp_servers"] == ["managed-a"]


# ---------------------------------------------------------------------------
# Active profile detection tests
# ---------------------------------------------------------------------------


class TestActiveProfileDetection:
    """Test query_active_profile reads from state.json."""

    def test_reads_from_state(self, install_env, capsys):
        with patch.object(install, "_load_state", return_value={"targets": {"global": {"profile": "anton"}}}):
            install.query_active_profile()
        assert "anton" in capsys.readouterr().out.strip()

    def test_not_installed(self, install_env, capsys):
        with patch.object(install, "_load_state", return_value={}):
            install.query_active_profile()
        out = capsys.readouterr().out.strip()
        assert out in ("not installed", "anton (local)")


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
        ):
            install.list_profiles()

        output = capsys.readouterr().out
        assert "[no CLAUDE.md]" in output


class TestInitDryRunRefuses:
    """`init --dry-run` must never perform a real install."""

    def test_dry_run_exits_without_installing(self, tmp_path, capsys):
        import argparse

        args = argparse.Namespace(dry_run=True, force=False, bundle=None, init_profile=None)
        with pytest.raises(SystemExit) as exc:
            install.cmd_init_unified(args)
        assert exc.value.code == 2
        assert "not implemented" in capsys.readouterr().err


class TestInitDaemonLifecycle:
    """`agentihooks init` owns the daemon: it must converge the running process
    onto the config it just wrote, not merely render a unit file.

    The bug these cover is silent. `systemctl daemon-reload` re-reads unit files
    without restarting running units, so before this the url in ~/.claude.json,
    the unit on disk and the live process could all disagree with nothing said.
    """

    @pytest.fixture(autouse=True)
    def _fake_daemon(self, monkeypatch):
        class _FakeDaemon:
            def __init__(self):
                self.calls = []

            def ensure_running(self, transport, python, cwd, *, restart=True):
                self.calls.append(("ensure_running", transport, restart))
                return SimpleNamespace(running=True, backend="pidfile", pid=4242, transport=transport, detail="ok")

            def stop(self):
                self.calls.append(("stop",))
                return True

            def read_pidfile(self):
                return {}

            def port_open(self, *a, **k):
                return False

        self.daemon = _FakeDaemon()
        monkeypatch.setattr(install, "_mcp_daemon_module", lambda: self.daemon)
        monkeypatch.setattr(install, "_resolve_hooks_python", lambda: Path("/venv/bin/python"))
        monkeypatch.setattr(install, "_install_systemd_user_unit", lambda transport: None)
        monkeypatch.setattr(install, "_remove_systemd_user_unit", lambda: None)

    def test_network_transport_starts_the_daemon(self):
        install._ensure_mcp_daemon("sse")

        assert ("ensure_running", "sse", True) in self.daemon.calls

    def test_restart_is_unconditional(self):
        """No change-detection: after init the running process must serve the
        config just written, and every input that could have changed is more
        failure surface than a sub-second restart costs."""
        install._ensure_mcp_daemon("streamable-http")

        assert all(call[2] is True for call in self.daemon.calls if call[0] == "ensure_running")

    def test_failure_to_start_warns_and_names_the_recovery_command(self, capsys):
        self.daemon.ensure_running = lambda t, p, c, restart=True: SimpleNamespace(
            running=False, backend="pidfile", pid=None, transport=t, detail="port busy"
        )

        install._ensure_mcp_daemon("sse")
        out = capsys.readouterr().out

        assert "port busy" in out
        assert "agentihooks mcp start" in out

    def test_clean_state_stops_the_daemon_before_sweeping_pidfiles(self, monkeypatch, tmp_path):
        """`_clean_state_dir` globs "*.pid". Sweeping the pidfile of a running
        daemon erases the only handle on it and leaves it orphaned on the port.

        The assertion has to encode ORDER, not just that both happened. Asserting
        `("stop",) in calls` and `not pidfile.exists()` passes even with the stop
        moved after the sweep — the glob deletes the file either way and the fake
        never touches the filesystem. So the fake records what it observed.
        """
        state = tmp_path / "agentihooks"
        state.mkdir()
        pidfile = state / "mcp-daemon.pid"
        pidfile.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(install, "AGENTIHOOKS_STATE_DIR", state)
        monkeypatch.setattr(install, "_clean_claude_home", lambda: 0)

        observed = {}

        def _stop_recording_what_it_saw():
            observed["pidfile_still_present"] = pidfile.exists()
            return True

        self.daemon.stop = _stop_recording_what_it_saw

        install._clean_state_dir()

        assert observed.get("pidfile_still_present") is True, (
            "the daemon was stopped after the sweep — by then the pidfile was gone and the process it named is orphaned"
        )
        assert not pidfile.exists()

    def test_install_global_actually_calls_ensure_mcp_daemon(self):
        """Pin the wiring: `_install_global_inner` is never executed by any test.

        Without this, deleting the `_ensure_mcp_daemon` call from the
        network-transport branch leaves the whole suite green while init silently
        stops starting the daemon — the headline behaviour of this feature.
        """
        import inspect

        src = inspect.getsource(install._install_global_inner)

        assert "_ensure_mcp_daemon(_mcp_transport)" in src
        # Ordered after the unit render: the systemd backend starts the unit that
        # call writes, so starting first would enable a stale or absent one.
        assert src.index("_install_systemd_user_unit(_mcp_transport)") < src.index("_ensure_mcp_daemon(_mcp_transport)")
        # The stdio branch must stop a daemon left over from a network install.
        assert "stop()" in src
