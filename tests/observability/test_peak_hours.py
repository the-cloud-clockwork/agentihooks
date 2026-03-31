"""Tests for hooks.observability.peak_hours."""

from datetime import datetime
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestPeakHours:
    """Tests for peak/off-peak detection."""

    def test_weekday_peak_hours(self):
        from hooks.observability.peak_hours import is_peak_now

        # Mock Tuesday at 10am Pacific
        with patch("hooks.observability.peak_hours.datetime") as mock_dt:
            from zoneinfo import ZoneInfo

            mock_dt.now.return_value = datetime(2026, 3, 31, 10, 0, tzinfo=ZoneInfo("US/Pacific"))
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert is_peak_now(9, 17, "US/Pacific") is True

    def test_weekday_off_peak(self):
        from hooks.observability.peak_hours import is_peak_now

        with patch("hooks.observability.peak_hours.datetime") as mock_dt:
            from zoneinfo import ZoneInfo

            mock_dt.now.return_value = datetime(2026, 3, 31, 20, 0, tzinfo=ZoneInfo("US/Pacific"))
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert is_peak_now(9, 17, "US/Pacific") is False

    def test_weekend_never_peak(self):
        from hooks.observability.peak_hours import is_peak_now

        # Saturday at 10am — should NOT be peak
        with patch("hooks.observability.peak_hours.datetime") as mock_dt:
            from zoneinfo import ZoneInfo

            mock_dt.now.return_value = datetime(2026, 3, 28, 10, 0, tzinfo=ZoneInfo("US/Pacific"))  # Saturday
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert is_peak_now(9, 17, "US/Pacific") is False

    def test_peak_indicator_returns_string(self):
        from hooks.observability.peak_hours import peak_indicator

        result = peak_indicator()
        assert result in ("PEAK", "off-peak")

    def test_peak_warning_off_peak_returns_none(self):
        from hooks.observability.peak_hours import peak_warning

        with patch("hooks.observability.peak_hours.is_peak_now", return_value=False):
            assert peak_warning(80.0) is None

    def test_peak_warning_peak_high_usage(self):
        from hooks.observability.peak_hours import peak_warning

        with patch("hooks.observability.peak_hours.is_peak_now", return_value=True):
            result = peak_warning(60.0)
            assert result is not None
            assert "PEAK" in result

    def test_peak_warning_peak_low_usage(self):
        from hooks.observability.peak_hours import peak_warning

        with patch("hooks.observability.peak_hours.is_peak_now", return_value=True):
            result = peak_warning(30.0)
            assert result is None
