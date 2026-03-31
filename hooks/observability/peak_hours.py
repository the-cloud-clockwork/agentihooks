"""Peak/off-peak awareness — detects Anthropic peak billing hours.

Anthropic burns session budget faster during weekday peak hours.
This module provides indicators for the statusline and warnings.
"""

from datetime import datetime
from typing import Optional


def is_peak_now(start_hour: int = 9, end_hour: int = 17, tz_name: str = "US/Pacific") -> bool:
    """Check if current time is during peak hours (weekday, within hour range).

    Args:
        start_hour: Peak start hour (inclusive), 0-23
        end_hour: Peak end hour (exclusive), 0-23
        tz_name: IANA timezone name for peak hour calculation

    Returns:
        True if currently in peak hours.
    """
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name)
    except (ImportError, KeyError):
        # zoneinfo not available or bad tz — fall back to UTC
        from datetime import timezone

        tz = timezone.utc

    now = datetime.now(tz)

    # Weekday: Monday=0 ... Friday=4
    if now.weekday() > 4:
        return False

    return start_hour <= now.hour < end_hour


def peak_indicator(start_hour: int = 9, end_hour: int = 17, tz_name: str = "US/Pacific") -> str:
    """Return compact peak status string for statusline."""
    if is_peak_now(start_hour, end_hour, tz_name):
        return "PEAK"
    return "off-peak"


def peak_warning(
    session_pct: float, start_hour: int = 9, end_hour: int = 17, tz_name: str = "US/Pacific"
) -> Optional[str]:
    """Return warning string if peak hours AND session usage is high.

    Args:
        session_pct: Current session usage percentage (0-100)

    Returns:
        Warning string or None.
    """
    if not is_peak_now(start_hour, end_hour, tz_name):
        return None
    if session_pct > 50:
        return "PEAK — sessions burn faster during business hours"
    return None
