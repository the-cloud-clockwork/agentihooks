"""Tests for hooks.context.compact_advisor."""

import pytest

pytestmark = pytest.mark.unit


class TestCompactAdvisor:
    """Tests for smart compact suggestion engine."""

    def test_suggest_compact_focus_with_data(self):
        from hooks.context.compact_advisor import suggest_compact_focus

        audit = {"Read": 50000, "Bash": 30000, "Agent": 20000}
        result = suggest_compact_focus(audit)
        assert "Read" in result
        assert "50K" in result
        assert "top consumers" in result

    def test_suggest_compact_focus_empty(self):
        from hooks.context.compact_advisor import suggest_compact_focus

        assert suggest_compact_focus({}) == ""

    def test_format_suggestion_warning_with_audit(self):
        from hooks.context.compact_advisor import format_suggestion

        audit = {"Read": 50000, "Bash": 30000}
        result = format_suggestion(65.0, "warning", audit)
        assert "65%" in result
        assert "/compact" in result
        assert "Read" in result

    def test_format_suggestion_critical_with_audit(self):
        from hooks.context.compact_advisor import format_suggestion

        audit = {"Read": 50000}
        result = format_suggestion(85.0, "critical", audit)
        assert "85%" in result
        assert "/compact now" in result

    def test_format_suggestion_no_audit_fallback(self):
        from hooks.context.compact_advisor import format_suggestion

        result = format_suggestion(65.0, "warning", None)
        assert "65%" in result
        assert "/compact" in result
        assert "top consumers" not in result

    def test_format_suggestion_critical_no_audit(self):
        from hooks.context.compact_advisor import format_suggestion

        result = format_suggestion(85.0, "critical", None)
        assert "/compact now" in result

    def test_top_3_only(self):
        from hooks.context.compact_advisor import suggest_compact_focus

        audit = {"Read": 50000, "Bash": 30000, "Agent": 20000, "Edit": 10000, "Write": 5000}
        result = suggest_compact_focus(audit)
        # Should only show top 3
        assert "Write" not in result
        assert "Read" in result
