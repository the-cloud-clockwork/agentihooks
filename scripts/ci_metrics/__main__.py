"""Measures one hour of agentihooks CI runner cost and lead time and writes the rows to ClickHouse.

The hourly CronJob in the agentihooks-swarm namespace runs it from the swarm image. The rows go through the metrics
outbox to the ci_cost and ci_lead_time tables; a run that ships fewer rows than it built fails.
"""

import argparse
import json
import os
import tempfile
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from scripts import ci_minutes
from scripts.ci_metrics import report, runs
from scripts.swarm import metrics_outbox

LAG_HOURS = 2
OPENED = datetime.min.replace(tzinfo=UTC)
FLUSHES = 3


def ship(
    sink: metrics_outbox.Settings, batches: Mapping[metrics_outbox.Table, list[dict[str, Any]]], now_ms: int
) -> int:
    """Ship every row through a fresh outbox, flushing again while rows wait, and return how many reached the sink."""
    with tempfile.TemporaryDirectory() as folder:
        box = metrics_outbox.Outbox(Path(folder) / metrics_outbox.spool_path().name, sink)
        try:
            for table, found in batches.items():
                box.append(table, found)  # pyright: ignore[reportUnknownMemberType]
            built = sum(len(found) for found in batches.values())
            shipped = 0
            for _ in range(FLUSHES):
                shipped += box.flush(now_ms)  # pyright: ignore[reportUnknownMemberType]
                if shipped == built:
                    break
            return shipped
        finally:
            box.close()


def end_of_window(until: str | None, lag_hours: int, now: datetime) -> datetime:
    """The end of the hour to measure: the given time, or the hour boundary the lag leaves behind now."""
    if until:
        return datetime.strptime(until, ci_minutes.STAMP).replace(tzinfo=UTC)
    return runs.lagged(now, timedelta(hours=lag_hours))


def main(argv: list[str] | None = None) -> int:
    """Read one window, compute its rows, ship them and print the headline numbers."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--prometheus", required=True)
    parser.add_argument("--lag-hours", type=int, default=LAG_HOURS)
    parser.add_argument("--until")
    args = parser.parse_args(argv)
    sink = metrics_outbox.settings(os.environ)  # pyright: ignore[reportUnknownMemberType]
    if sink is None:
        raise SystemExit("ci metrics refused: the metrics sink settings are not set")
    since, until = runs.window(end_of_window(args.until, args.lag_hours, datetime.now(UTC)))
    pull = runs.read(args.repo, since, until)
    tiers = report.by_tier(report.tallies(pull.runs, pull.jobs))
    leads = report.lead_times(pull.merged, pull.branch_runs, OPENED)
    pending = runs.pending(args.prometheus, until)
    batches = report.rows(args.repo, since, tiers, len(pull.merged), leads, pending)
    built = sum(len(found) for found in batches.values())
    shipped = ship(sink, batches, round(time.time() * 1000))
    print(json.dumps({"since": since.isoformat(), "until": until.isoformat(), "rows": built, "shipped": shipped}))
    print(json.dumps(report.summary(tiers, len(pull.merged), leads, pending)))
    if shipped != built:
        raise SystemExit(f"ci metrics failed: shipped {shipped} of {built} rows to the metrics sink")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
