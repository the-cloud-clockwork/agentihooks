# pyright: reportUnusedImport=false
"""Measures one hour of agentihooks CI runner cost and lead time and writes the rows to ClickHouse.

The hourly CronJob in the agentihooks-swarm namespace runs it from the swarm image. The rows go through the metrics
outbox to the ci_cost and ci_lead_time tables; a run that ships fewer rows than it built fails.
"""

import argparse  # noqa: F401
import json  # noqa: F401
import os  # noqa: F401
import tempfile  # noqa: F401
import time  # noqa: F401
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any

from scripts import ci_minutes  # noqa: F401
from scripts.ci_metrics import report, runs  # noqa: F401
from scripts.swarm import metrics_outbox

LAG_HOURS = 2
OPENED = datetime.min.replace(tzinfo=UTC)


def ship(
    sink: metrics_outbox.Settings, batches: Mapping[metrics_outbox.Table, list[dict[str, Any]]], now_ms: int
) -> int:
    """Ship every row through a fresh outbox and return how many reached the sink."""
    raise NotImplementedError()


def end_of_window(until: str | None, lag_hours: int, now: datetime) -> datetime:
    """The end of the hour to measure: the given time, or the hour boundary the lag leaves behind now."""
    raise NotImplementedError()


def main(argv: list[str] | None = None) -> int:
    """Read one window, compute its rows, ship them and print the headline numbers."""
    raise NotImplementedError()


if __name__ == "__main__":
    raise SystemExit(main())
