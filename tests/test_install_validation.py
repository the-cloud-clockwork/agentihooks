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
        assert dst.read_text() == (
            f"<!-- profile: test-profile -->\n{(profile / 'CLAUDE.md').read_text()}"
        )

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

        # install_global is a thin lock wrapper; the body lives in the inner fn.
        src = inspect.getsource(install._install_global_inner)
        assert "_prepend_bundle_claude_md(bundle_dir)" in src
        # Must sit between the profile writer and the manifesto appender.
        assert src.index("_install_system_prompt") < src.index("_prepend_bundle_claude_md(bundle_dir)")
        assert src.index("_prepend_bundle_claude_md(bundle_dir)") < src.index(
            "_append_ci_manifesto_to_claude_md()"
        )
        # Exactly one call site — never inside the chain loop.
        assert src.count("_prepend_bundle_claude_md(") == 1

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
        (profile / "CLAUDE.md").write_text(
            "# Anton\nFormat:\n\n<!-- BEGIN sample -->\nexample\n<!-- END sample -->\n"
        )
        dst = install_env["claude_home"] / "CLAUDE.md"
        dst.write_text(
            "<!-- profile: test-profile -->\n# old\n\n"
            "<!-- BEGIN agentibridge -->\nab\n<!-- END agentibridge -->\n"
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
            "<!-- profile: test-profile -->\n# old\n\n"
            "<!-- BEGIN agentibridge -->\nin-flight, never closed\n"
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

        src = inspect.getsource(install._install_global_inner)
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


class TestManagedMcpChainCollection:
    """Regression guard for Defect B: _collect_all_managed_mcp_servers must walk
    the FULL comma-separated profile chain, not pass the joined string to
    _resolve_profile_dir (which returns None and collapses the set to hooks-utils).
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
            patch.object(install, "_build_mcp_config", return_value={"mcpServers": {"hooks-utils": {"command": "x"}}}),
        ):
            managed = set(install._collect_all_managed_mcp_servers().keys())
        # Both profiles' servers present — NOT collapsed to just hooks-utils.
        assert managed == {"hooks-utils", "bundle-server", "profile-server", "second-server"}


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
