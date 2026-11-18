import gzip
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts.ci_metrics import report
from scripts.swarm import metrics_outbox

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).parent / "fixtures" / "agent-scale-2026-11-04.json.gz"
REPO = "the-cloud-clockwork/agentihooks"
TEST = ".github/workflows/test.yml"
START = datetime(2026, 11, 4, 14, tzinfo=UTC)
OPENED = datetime.min.replace(tzinfo=UTC)
START_MS = 1793800800000
BASE = {"ledger": REPO, "plan": "", "phase": "", "slice": "", "task": ""}


def pulled() -> dict[str, Any]:
    return json.loads(gzip.decompress(FIXTURE.read_bytes()))


def run(
    id: int,
    event: str = "pull_request",
    conclusion: str = "success",
    created: str = "2026-11-04T10:00:00Z",
    sha: str = "a",
) -> dict[str, Any]:
    return {
        "id": id,
        "event": event,
        "head_branch": "feature",
        "conclusion": conclusion,
        "created_at": created,
        "head_sha": sha,
        "path": TEST,
    }


def job(id: int, run_id: int, seconds: int, conclusion: str = "success") -> dict[str, Any]:
    minute, second = divmod(seconds, 60)
    return {
        "id": id,
        "run_id": run_id,
        "conclusion": conclusion,
        "started_at": "2026-11-04T10:00:00Z",
        "completed_at": f"2026-11-04T10:{minute:02d}:{second:02d}Z",
    }


def window() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    runs = [run(1), run(2, conclusion="cancelled"), run(3), run(4, event="merge_group")]
    jobs = [
        job(10, 1, 180),
        job(11, 1, 60),
        job(11, 1, 60),
        job(12, 1, 3000, conclusion="skipped"),
        job(20, 2, 120),
        job(40, 4, 600),
    ]
    return runs, jobs


def pull_request(merged_at: str = "2026-11-04T11:30:00Z") -> dict[str, Any]:
    return {"number": 7, "branch": "feature", "created_at": "2026-11-04T10:05:00Z", "merged_at": merged_at}


def test_the_baseline_pull_reproduces_its_lead_time_and_heads_per_branch() -> None:
    body = pulled()
    leads = report.lead_times(body["merged"], body["runs"], report.at(body["since"]))
    found = report.summary({}, 0, leads, {})
    assert found["lead_time"] == {"pull_requests": 167, "p50_minutes": 54.0, "p90_minutes": 360.5}
    assert found["heads"] == {"p50": 3.0, "p90": 9.0, "max": 27.0}


def test_the_baseline_pull_costs_about_717_runner_minutes_per_merged_pull_request() -> None:
    body = pulled()
    since, until = report.at(body["since"]), report.at(body["until"])
    merged = sum(since <= report.at(item["merged_at"]) <= until for item in body["merged"])
    found = report.summary(report.by_tier(report.tallies(body["runs"], body["jobs"])), merged, [], {})
    assert (found["merged"], found["runs"], found["read_runs"]) == (169, 5532, 4432)
    assert (found["runner_minutes"], found["per_merged"]) == (121112.9, 716.6)
    assert (found["cancelled_share"], found["cancelled_minutes"], found["cancelled_minutes_share"]) == (
        0.094,
        29285.2,
        0.25,
    )
    assert {name: tier["per_merged"] for name, tier in found["tiers"].items()} == {
        "branch push": 156.5,
        "promotion": 113.8,
        "pull request": 339.7,
        "queue": 87.3,
        "release": 3.7,
        "schedule": 2.9,
        "workflow_dispatch": 12.1,
        "workflow_run": 0.6,
    }


def test_a_skipped_job_spends_no_runner_minutes() -> None:
    assert report.job_minutes(job(1, 1, 600, conclusion="skipped")) == 0.0
    assert report.job_minutes(job(1, 1, 90)) == 1.5


def test_a_job_listed_twice_counts_once_toward_its_run() -> None:
    assert report.minutes_by_run([job(1, 5, 60), job(1, 5, 60), job(2, 5, 120), job(3, 6, 30)]) == {5: 3.0, 6: 0.5}


def test_unread_runs_count_at_the_minutes_per_run_of_their_workflows_read_runs() -> None:
    runs, jobs = window()
    assert report.tallies(runs, jobs) == {
        ("pull request", TEST): report.Tally(
            runs=3, read=2, minutes=6.0, estimated=9.0, cancelled_runs=1, cancelled_minutes=2.0
        ),
        ("queue", TEST): report.Tally(runs=1, read=1, minutes=10.0, estimated=10.0),
    }


def test_a_workflow_with_no_read_run_estimates_nothing() -> None:
    found = report.tally([run(1), run(2, conclusion="cancelled")], {})
    assert found == report.Tally(runs=2, cancelled_runs=1)


def test_workflow_tallies_sum_into_their_tier() -> None:
    first = report.Tally(runs=1, read=1, minutes=2.0, estimated=2.0, cancelled_runs=1, cancelled_minutes=2.0)
    second = report.Tally(runs=3, read=2, minutes=4.0, estimated=6.0)
    third = report.Tally(runs=5, read=5, minutes=1.0, estimated=1.0)
    found = report.by_tier({("queue", "a.yml"): first, ("queue", "b.yml"): second, ("promotion", "a.yml"): third})
    assert found == {
        "queue": report.Tally(runs=4, read=3, minutes=6.0, estimated=8.0, cancelled_runs=1, cancelled_minutes=2.0),
        "promotion": third,
    }


def test_lead_time_runs_from_the_first_branch_run_to_the_merge_and_counts_heads_before_it() -> None:
    runs = [
        run(1, created="2026-11-04T10:00:00Z", sha="a"),
        run(2, created="2026-11-04T10:30:00Z", sha="b"),
        run(3, created="2026-11-04T10:45:00Z", sha="b"),
        run(4, created="2026-11-04T12:00:00Z", sha="c"),
        {**run(5, created="2026-11-04T09:00:00Z", sha="d"), "head_branch": "other"},
    ]
    found = report.lead_times([pull_request()], runs, OPENED)
    assert found == [
        report.Lead(
            7,
            "feature",
            datetime(2026, 11, 4, 10, tzinfo=UTC),
            datetime(2026, 11, 4, 11, 30, tzinfo=UTC),
            heads=2,
            runs=3,
        )
    ]
    assert found[0].minutes == 90.0


def test_a_branch_whose_first_run_is_not_after_the_record_opened_has_no_lead_time() -> None:
    opened = datetime(2026, 11, 4, 10, tzinfo=UTC)
    assert report.lead(pull_request(), [run(1, created="2026-11-04T10:00:00Z")], opened) is None
    assert report.lead(pull_request(), [run(1, created="2026-11-04T10:00:01Z")], opened) is not None


def test_a_branch_with_no_run_before_its_merge_has_no_lead_time() -> None:
    assert report.lead(pull_request(), [run(1, created="2026-11-04T11:30:01Z")], OPENED) is None
    assert report.lead(pull_request(), [run(1, created="2026-11-04T11:30:00Z")], OPENED) is not None
    assert report.lead_times([pull_request()], [], OPENED) == []


def test_percentiles_interpolate_between_the_closest_ranks() -> None:
    assert report.percentile([4.0, 1.0, 3.0, 2.0], 0.5) == 2.5
    assert report.percentile([4.0, 1.0, 3.0, 2.0], 0.9) == pytest.approx(3.7)
    assert report.percentile([5.0], 0.9) == 5.0
    assert report.percentile([], 0.5) is None


def test_lead_time_percentiles_print_to_a_tenth_of_a_minute() -> None:
    merged = datetime(2026, 11, 4, 11, 30, 15, tzinfo=UTC)
    found = report.summary({}, 0, [report.Lead(7, "feature", START.replace(hour=10), merged, heads=1, runs=1)], {})
    assert found["lead_time"] == {"pull_requests": 1, "p50_minutes": 90.2, "p90_minutes": 90.2}


def test_the_summary_reports_cost_shares_lead_time_heads_and_pending_runners() -> None:
    runs, jobs = window()
    tiers = report.by_tier(report.tallies(runs, jobs))
    leads = report.lead_times([pull_request()], runs[:3], OPENED)
    found = report.summary(tiers, 2, leads, {"anton-oss": 1.234, "anton-oss-batch": 0.5})
    assert found == {
        "merged": 2,
        "runs": 4,
        "read_runs": 3,
        "runner_minutes": 19.0,
        "per_merged": 9.5,
        "cancelled_share": 0.25,
        "cancelled_minutes": 2.0,
        "cancelled_minutes_share": 0.125,
        "tiers": {
            "pull request": {
                "runs": 3,
                "read_runs": 2,
                "runner_minutes": 9.0,
                "per_merged": 4.5,
                "cancelled_share": 0.333,
                "cancelled_minutes": 2.0,
                "cancelled_minutes_share": 0.333,
            },
            "queue": {
                "runs": 1,
                "read_runs": 1,
                "runner_minutes": 10.0,
                "per_merged": 5.0,
                "cancelled_share": 0.0,
                "cancelled_minutes": 0.0,
                "cancelled_minutes_share": 0.0,
            },
        },
        "lead_time": {"pull_requests": 1, "p50_minutes": 90.0, "p90_minutes": 90.0},
        "heads": {"p50": 1.0, "p90": 1.0, "max": 1.0},
        "pending_runners": {"anton-oss": 1.23, "anton-oss-batch": 0.5},
    }


def test_an_empty_window_summarises_to_zero_without_dividing_by_zero() -> None:
    found = report.summary({}, 0, [], {})
    assert (found["per_merged"], found["cancelled_share"], found["cancelled_minutes_share"]) == (0.0, 0.0, 0.0)
    assert found["lead_time"] == {"pull_requests": 0, "p50_minutes": None, "p90_minutes": None}
    assert found["heads"] == {"p50": None, "p90": None, "max": None}


def test_a_window_writes_one_cost_row_per_window_tier_and_pool_and_one_lead_row_per_merged_pull_request() -> None:
    runs, jobs = window()
    tiers = report.by_tier(report.tallies(runs, jobs))
    leads = report.lead_times([pull_request()], runs[:3], OPENED)
    found = report.rows(REPO, START, tiers, 2, leads, {"anton-oss": 1.5, "anton-oss-batch": 0.5})

    def cost(scope: str, name: str, **values: Any) -> dict[str, Any]:
        zero = {
            "runs": 0,
            "read_runs": 0,
            "runner_minutes": 0.0,
            "estimated_minutes": 0.0,
            "cancelled_runs": 0,
            "cancelled_minutes": 0.0,
            "per_merged": 0.0,
            "pending_runners": 0.0,
        }
        return {
            **BASE,
            "event_id": f"ci-cost:{REPO}:2026-11-04T14:00:00+00:00:{scope}:{name}",
            "ts_ms": START_MS,
            "scope": scope,
            "name": name,
            "merged": 2,
            **zero,
            **values,
        }

    assert found == {
        report.COST: [
            cost(
                "window",
                "all",
                runs=4,
                read_runs=3,
                runner_minutes=16.0,
                estimated_minutes=19.0,
                cancelled_runs=1,
                cancelled_minutes=2.0,
                per_merged=9.5,
                pending_runners=2.0,
            ),
            cost(
                "tier",
                "pull request",
                runs=3,
                read_runs=2,
                runner_minutes=6.0,
                estimated_minutes=9.0,
                cancelled_runs=1,
                cancelled_minutes=2.0,
                per_merged=4.5,
            ),
            cost("tier", "queue", runs=1, read_runs=1, runner_minutes=10.0, estimated_minutes=10.0, per_merged=5.0),
            cost("pool", "anton-oss", pending_runners=1.5),
            cost("pool", "anton-oss-batch", pending_runners=0.5),
        ],
        report.LEAD: [
            {
                **BASE,
                "event_id": f"ci-lead:{REPO}:7",
                "ts_ms": 1793791800000,
                "pr": 7,
                "branch": "feature",
                "first_run_ms": 1793786400000,
                "merged_ms": 1793791800000,
                "minutes": 90.0,
                "heads": 1,
                "runs": 3,
            }
        ],
    }


def test_the_tables_are_ci_cost_and_ci_lead_time_in_the_swarm_database() -> None:
    assert report.COST.ddl().startswith(f"CREATE TABLE IF NOT EXISTS {metrics_outbox.DATABASE}.ci_cost (")
    assert report.LEAD.ddl().startswith(f"CREATE TABLE IF NOT EXISTS {metrics_outbox.DATABASE}.ci_lead_time (")


def test_every_row_carries_exactly_the_columns_of_its_table() -> None:
    runs, jobs = window()
    tiers = report.by_tier(report.tallies(runs, jobs))
    leads = report.lead_times([pull_request()], runs[:3], OPENED)
    found = report.rows(REPO, START, tiers, 2, leads, {"anton-oss": 1.5})
    assert [len(table_rows) for table_rows in found.values()] == [4, 1]
    for table, table_rows in found.items():
        for row in table_rows:
            table.check(row)
