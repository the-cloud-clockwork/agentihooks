"""Tests for hooks.context.ci_manifesto."""

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Size-aware injection (budget guard for Claude Code 10K cap)
# ---------------------------------------------------------------------------


class TestInjectionBudget:
    def test_oversize_manifesto_truncates_with_path_footer(self, tmp_path, monkeypatch):
        from unittest.mock import patch

        from hooks.context import ci_manifesto

        manifest = tmp_path / "huge.md"
        manifest.write_text("# Doctrine\n" + ("padding " * 5000))  # ~40 KB
        with patch.object(ci_manifesto, "_load") as mock_load:
            mock_load.return_value = {"path": str(manifest), "content": manifest.read_text()}
            payload = ci_manifesto._build_injection()
        assert len(payload.encode("utf-8")) <= ci_manifesto._INJECTION_BUDGET_BYTES
        assert "[TRUNCATED" in payload
        assert str(manifest) in payload

    def test_under_budget_manifesto_emitted_full(self, tmp_path):
        from unittest.mock import patch

        from hooks.context import ci_manifesto

        small = tmp_path / "small.md"
        small.write_text("# Doctrine\n\nbe nice.")
        with patch.object(ci_manifesto, "_load") as mock_load:
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

