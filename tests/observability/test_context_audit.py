"""Tests for hooks.observability.context_audit."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestContextAudit:
    """Tests for context audit tracking."""

    def setup_method(self):
        from hooks.observability import context_audit
        context_audit._memory_audit.clear()

    def teardown_method(self):
        from hooks.observability import context_audit
        context_audit._memory_audit.clear()

    def test_record_and_get_summary(self):
        from hooks.observability.context_audit import record_tool_usage, get_audit_summary

        sid = "audit-test-record-001"
        with patch("hooks._redis.get_redis", return_value=None):
            with patch("hooks.observability.context_audit.get_redis", return_value=None):
                record_tool_usage(sid, "Read", 1000)
                record_tool_usage(sid, "Bash", 2000)
                record_tool_usage(sid, "Read", 500)

                summary = get_audit_summary(sid)
                assert summary["Read"] == 1500
                assert summary["Bash"] == 2000

    def test_empty_session(self):
        from hooks.observability.context_audit import get_audit_summary

        with patch("hooks.observability.context_audit.get_redis", return_value=None):
            assert get_audit_summary("audit-test-nonexist-002") == {}

    def test_ignores_zero_or_negative(self):
        from hooks.observability.context_audit import record_tool_usage, get_audit_summary

        sid = "audit-test-zero-003"
        with patch("hooks.observability.context_audit.get_redis", return_value=None):
            record_tool_usage(sid, "Read", 0)
            record_tool_usage(sid, "Read", -5)
            assert get_audit_summary(sid) == {}

    def test_ignores_empty_params(self):
        from hooks.observability.context_audit import record_tool_usage, get_audit_summary

        with patch("hooks.observability.context_audit.get_redis", return_value=None):
            record_tool_usage("", "Read", 100)
            record_tool_usage("audit-test-empty-004", "", 100)
            assert get_audit_summary("audit-test-empty-004") == {}

    def test_format_audit_report(self):
        from hooks.observability.context_audit import format_audit_report

        summary = {"Read": 50000, "Bash": 30000, "Agent": 20000}
        report = format_audit_report(summary, 75.0)
        assert "Context audit" in report
        assert "75%" in report
        assert "Read" in report
        assert "50K" in report

    def test_format_audit_report_empty(self):
        from hooks.observability.context_audit import format_audit_report

        assert format_audit_report({}, 50.0) == ""

    def test_clear_session_audit(self):
        from hooks.observability.context_audit import record_tool_usage, get_audit_summary, clear_session_audit

        sid = "audit-test-clear-005"
        with patch("hooks.observability.context_audit.get_redis", return_value=None):
            record_tool_usage(sid, "Read", 1000)
            clear_session_audit(sid)
            assert get_audit_summary(sid) == {}

    def test_session_isolation(self):
        from hooks.observability.context_audit import record_tool_usage, get_audit_summary

        with patch("hooks.observability.context_audit.get_redis", return_value=None):
            record_tool_usage("audit-test-iso-a-006", "Read", 1000)
            record_tool_usage("audit-test-iso-b-006", "Read", 2000)

            assert get_audit_summary("audit-test-iso-a-006")["Read"] == 1000
            assert get_audit_summary("audit-test-iso-b-006")["Read"] == 2000
