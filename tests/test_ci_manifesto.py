"""Tests for hooks.context.ci_manifesto."""

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Size-aware injection (budget guard for Claude Code 10K cap)
# ---------------------------------------------------------------------------


class TestInjectionBudget:
    def test_default_no_cap_emits_full_doctrine(self, tmp_path):
        """Default CI_MANIFESTO_MAX_BYTES=0 → no truncation, full content reaches model."""
        from unittest.mock import patch

        from hooks.context import ci_manifesto

        manifest = tmp_path / "huge.md"
        manifest.write_text("# Doctrine\n" + ("padding " * 5000))  # ~40 KB
        with patch.object(ci_manifesto, "_load") as mock_load, patch(
            "hooks.config.CI_MANIFESTO_MAX_BYTES", 0
        ):
            mock_load.return_value = {"path": str(manifest), "content": manifest.read_text()}
            payload = ci_manifesto._build_injection()
        assert "[TRUNCATED" not in payload
        assert "padding " * 100 in payload  # body fully present

    def test_opt_in_cap_truncates_with_path_footer(self, tmp_path):
        """When CI_MANIFESTO_MAX_BYTES is set, oversized content is truncated."""
        from unittest.mock import patch

        from hooks.context import ci_manifesto

        manifest = tmp_path / "huge.md"
        manifest.write_text("# Doctrine\n" + ("padding " * 5000))  # ~40 KB
        with patch.object(ci_manifesto, "_load") as mock_load, patch(
            "hooks.config.CI_MANIFESTO_MAX_BYTES", 7500
        ):
            mock_load.return_value = {"path": str(manifest), "content": manifest.read_text()}
            payload = ci_manifesto._build_injection()
        assert len(payload.encode("utf-8")) <= 7500
        assert "[TRUNCATED" in payload
        assert str(manifest) in payload

    def test_under_budget_no_truncation(self, tmp_path):
        from unittest.mock import patch

        from hooks.context import ci_manifesto

        small = tmp_path / "small.md"
        small.write_text("# Doctrine\n\nbe nice.")
        with patch.object(ci_manifesto, "_load") as mock_load, patch(
            "hooks.config.CI_MANIFESTO_MAX_BYTES", 7500
        ):
            mock_load.return_value = {"path": str(small), "content": small.read_text()}
            payload = ci_manifesto._build_injection()
        assert "be nice." in payload
        assert "[TRUNCATED" not in payload
        assert "=== END CI MANIFESTO ===" in payload

    def test_empty_content_returns_empty(self, tmp_path):
        from unittest.mock import patch

        from hooks.context import ci_manifesto

        with patch.object(ci_manifesto, "_load") as mock_load:
            mock_load.return_value = {"path": "/nope", "content": ""}
            assert ci_manifesto._build_injection() == ""

