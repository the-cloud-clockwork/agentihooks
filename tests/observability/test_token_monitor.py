"""Tests for hooks.observability.token_monitor."""

from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


def _payload(used: int, remaining: int, session_id: str = "sess-test", model: str = "sonnet-4.6") -> dict:
    return {
        "session_id": session_id,
        "model": model,
        "context_window": {"used": used, "remaining": remaining},
    }


class TestTokenMonitor:
    """Tests for context window metrics and status line generation."""

    def test_fill_pct_calculation(self):
        """used=234000, remaining=766000 → 23.4%."""
        from hooks.observability.token_monitor import get_context_fill_pct

        p = _payload(234000, 766000)
        result = get_context_fill_pct(p)
        assert result is not None
        assert abs(result - 23.4) < 0.1

    def test_fill_pct_missing_context_window(self):
        """Missing context_window → None."""
        from hooks.observability.token_monitor import get_context_fill_pct

        assert get_context_fill_pct({}) is None
        assert get_context_fill_pct({"context_window": None}) is None

    def test_below_warn_threshold(self):
        """fill_pct=50 (< 60) → (False, '')."""
        from hooks.observability.token_monitor import should_warn_context

        with patch("hooks._redis.get_redis", return_value=None):
            result = should_warn_context(50.0, "sess-below")
        assert result == (False, "")

    def test_at_warn_threshold(self):
        """fill_pct=60 → (True, 'warning')."""
        from hooks.observability.token_monitor import should_warn_context

        with patch("hooks._redis.get_redis", return_value=None):
            warn, level = should_warn_context(60.0, "sess-warn")
        assert warn is True
        assert level == "warning"

    def test_above_warn_below_critical(self):
        """fill_pct=70 → (True, 'warning')."""
        from hooks.observability.token_monitor import should_warn_context

        with patch("hooks._redis.get_redis", return_value=None):
            warn, level = should_warn_context(70.0, "sess-70")
        assert warn is True
        assert level == "warning"

    def test_at_critical_threshold(self):
        """fill_pct=80 → (True, 'critical')."""
        from hooks.observability.token_monitor import should_warn_context

        with patch("hooks._redis.get_redis", return_value=None):
            warn, level = should_warn_context(80.0, "sess-crit")
        assert warn is True
        assert level == "critical"

    def test_status_line_format(self):
        """Status line contains ctx:, %, and model name."""
        from hooks.observability.token_monitor import update_context_metrics

        p = _payload(234000, 766000, model="sonnet-4.6")
        with patch("hooks._redis.get_redis", return_value=None):
            result = update_context_metrics(p)
        assert "ctx:" in result
        assert "%" in result
        assert "sonnet-4.6" in result

    def test_status_line_k_format(self):
        """Used value formatted with K suffix."""
        from hooks.observability.token_monitor import update_context_metrics

        p = _payload(50_000, 950_000)
        with patch("hooks._redis.get_redis", return_value=None):
            result = update_context_metrics(p)
        assert "50K" in result

    def test_no_redis_graceful_fallback(self):
        """Redis unavailable → update_context_metrics still returns valid string."""
        from hooks.observability.token_monitor import update_context_metrics

        p = _payload(100_000, 900_000)
        with patch("hooks._redis.get_redis", return_value=None):
            result = update_context_metrics(p)
        assert isinstance(result, str)
        assert len(result) > 0
        assert "ctx:" in result

    def test_empty_payload_graceful(self):
        """Empty payload → returns placeholder string without raising."""
        from hooks.observability.token_monitor import update_context_metrics

        with patch("hooks._redis.get_redis", return_value=None):
            result = update_context_metrics({})
        assert isinstance(result, str)
        assert len(result) > 0

    def test_should_warn_no_session_id(self):
        """Empty session_id with Redis unavailable → still warns."""
        from hooks.observability.token_monitor import should_warn_context

        with patch("hooks._redis.get_redis", return_value=None):
            warn, level = should_warn_context(65.0, "")
        assert warn is True
        assert level == "warning"
