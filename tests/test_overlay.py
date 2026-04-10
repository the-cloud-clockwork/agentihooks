"""Tests for the runtime overlay system."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _clean_overlay_state(tmp_path):
    """Redirect overlay state to tmp dir."""
    overlay_file = tmp_path / "active_overlays.json"
    with patch("scripts.overlay.OVERLAYS_FILE", overlay_file):
        yield


@pytest.fixture()
def anton_profile(tmp_path):
    """Create a minimal anton profile with allowedOverlays."""
    profile_dir = tmp_path / "profiles" / "anton"
    profile_dir.mkdir(parents=True)
    (profile_dir / "profile.yml").write_text(
        "name: anton\ndescription: test\nallowedOverlays:\n  - patch-mode\n  - router\n"
    )
    (profile_dir / "CLAUDE.md").write_text("# Anton Profile\nTest content.")
    rules_dir = profile_dir / ".claude" / "rules"
    rules_dir.mkdir(parents=True)
    (rules_dir / "test-rule.md").write_text("# Test Rule\nDo the thing.")
    return profile_dir


@pytest.fixture()
def patch_profile(tmp_path):
    """Create a minimal patch-mode profile."""
    profile_dir = tmp_path / "profiles" / "patch-mode"
    profile_dir.mkdir(parents=True)
    (profile_dir / "profile.yml").write_text("name: patch-mode\ndescription: surgical mode\n")
    (profile_dir / "CLAUDE.md").write_text("# Patch Mode\nMake it work live.")
    return profile_dir


class TestOverlayAddRemove:
    def test_add_allowed_overlay(self, anton_profile, patch_profile):
        from scripts.overlay import overlay_add

        with patch("scripts.overlay._resolve_profile_dir", return_value=patch_profile):
            result = overlay_add("patch-mode", anton_profile, added_by="test")
        assert result["success"] is True
        assert result["overlay"]["name"] == "patch-mode"
        assert "Make it work live" in result["overlay"]["rules_content"]

    def test_add_disallowed_overlay(self, anton_profile):
        from scripts.overlay import overlay_add

        result = overlay_add("agenticore", anton_profile, added_by="test")
        assert result["success"] is False
        assert "not in allowedOverlays" in result["error"]

    def test_add_duplicate_overlay(self, anton_profile, patch_profile):
        from scripts.overlay import overlay_add

        with patch("scripts.overlay._resolve_profile_dir", return_value=patch_profile):
            overlay_add("patch-mode", anton_profile, added_by="test")
            result = overlay_add("patch-mode", anton_profile, added_by="test")
        assert result["success"] is False
        assert "already active" in result["error"]

    def test_remove_active_overlay(self, anton_profile, patch_profile):
        from scripts.overlay import overlay_add, overlay_remove

        with patch("scripts.overlay._resolve_profile_dir", return_value=patch_profile):
            overlay_add("patch-mode", anton_profile, added_by="test")
        result = overlay_remove("patch-mode")
        assert result["success"] is True

    def test_remove_inactive_overlay(self):
        from scripts.overlay import overlay_remove

        result = overlay_remove("nonexistent")
        assert result["success"] is False


class TestOverlayContent:
    def test_render_includes_claude_md_and_rules(self, anton_profile, patch_profile):
        from scripts.overlay import overlay_add

        with patch("scripts.overlay._resolve_profile_dir", return_value=patch_profile):
            result = overlay_add("patch-mode", anton_profile, added_by="test")
        content = result["overlay"]["rules_content"]
        assert "Patch Mode" in content
        assert "Make it work live" in content

    def test_get_overlay_content_format(self, anton_profile, patch_profile):
        from scripts.overlay import get_overlay_content, overlay_add

        with patch("scripts.overlay._resolve_profile_dir", return_value=patch_profile):
            overlay_add("patch-mode", anton_profile, added_by="test")
        content = get_overlay_content()
        assert content is not None
        assert "=== OVERLAY ACTIVE: patch-mode ===" in content
        assert "=== END OVERLAY: patch-mode ===" in content

    def test_get_overlay_content_empty(self):
        from scripts.overlay import get_overlay_content

        assert get_overlay_content() is None


class TestOverlayRefreshClear:
    def test_refresh_rerenders(self, anton_profile, patch_profile):
        from scripts.overlay import overlay_add, overlay_refresh

        with patch("scripts.overlay._resolve_profile_dir", return_value=patch_profile):
            overlay_add("patch-mode", anton_profile, added_by="test")
            # Modify the source
            (patch_profile / "CLAUDE.md").write_text("# Patch Mode V2\nUpdated content.")
            result = overlay_refresh("patch-mode")
        assert result["success"] is True
        assert "Updated content" in result["overlay"]["rules_content"]

    def test_clear_all(self, anton_profile, patch_profile):
        from scripts.overlay import get_active_overlays, overlay_add, overlay_clear

        with patch("scripts.overlay._resolve_profile_dir", return_value=patch_profile):
            overlay_add("patch-mode", anton_profile, added_by="test")
        result = overlay_clear()
        assert result["removed_count"] == 1
        assert get_active_overlays() == []


class TestAllowedOverlays:
    def test_reads_allowlist_from_profile_yml(self, anton_profile):
        from scripts.overlay import _get_allowed_overlays

        allowed = _get_allowed_overlays(anton_profile)
        assert "patch-mode" in allowed
        assert "router" in allowed
        assert "agenticore" not in allowed

    def test_empty_allowlist(self, tmp_path):
        from scripts.overlay import _get_allowed_overlays

        profile = tmp_path / "empty"
        profile.mkdir()
        (profile / "profile.yml").write_text("name: empty\n")
        assert _get_allowed_overlays(profile) == []
