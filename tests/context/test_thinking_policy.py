"""Tests for hooks.context.thinking_policy."""

import pytest

pytestmark = pytest.mark.unit


class TestThinkingPolicy:
    """Tests for thinking/effort policy guidance."""

    def test_medium_effort_generates_guidance(self):
        from hooks.context.thinking_policy import get_thinking_guidance

        result = get_thinking_guidance("medium", 0)
        assert "medium" in result.lower()
        assert "Sonnet" in result

    def test_low_effort_generates_guidance(self):
        from hooks.context.thinking_policy import get_thinking_guidance

        result = get_thinking_guidance("low", 0)
        assert "low" in result.lower()
        assert "minimal" in result.lower()

    def test_high_effort_no_limit_returns_empty(self):
        from hooks.context.thinking_policy import get_thinking_guidance

        result = get_thinking_guidance("high", 0)
        assert result == ""

    def test_high_effort_with_budget_returns_budget(self):
        from hooks.context.thinking_policy import get_thinking_guidance

        result = get_thinking_guidance("high", 10000)
        assert "10,000" in result

    def test_budget_included_in_output(self):
        from hooks.context.thinking_policy import get_thinking_guidance

        result = get_thinking_guidance("medium", 5000)
        assert "5,000" in result
        assert "budget" in result.lower()

    def test_unknown_effort_defaults_to_medium(self):
        from hooks.context.thinking_policy import get_thinking_guidance

        result = get_thinking_guidance("unknown", 0)
        assert "medium" in result.lower()


class TestSubagentEffortCheck:
    """Tests for subagent effort alignment checking."""

    def test_opus_with_medium_profile_warns(self):
        from hooks.context.thinking_policy import check_subagent_effort

        result = check_subagent_effort({"model": "opus"}, "medium")
        assert result is not None
        assert "opus" in result.lower()

    def test_sonnet_with_medium_profile_ok(self):
        from hooks.context.thinking_policy import check_subagent_effort

        result = check_subagent_effort({"model": "sonnet"}, "medium")
        assert result is None

    def test_opus_with_high_profile_ok(self):
        from hooks.context.thinking_policy import check_subagent_effort

        result = check_subagent_effort({"model": "opus"}, "high")
        assert result is None

    def test_no_model_ok(self):
        from hooks.context.thinking_policy import check_subagent_effort

        result = check_subagent_effort({}, "medium")
        assert result is None
