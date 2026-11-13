"""GitHub lists at most 1000 runs for one filtered query, so runs are read one hour at a time and a full hour fails."""

import argparse
import json
import os
import re
import subprocess
from collections import defaultdict
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from scripts.ci_storage import rows

HOUR = timedelta(hours=1)
LISTED_CAP = 1000
JOB_PAGE = 100
SMALL_JOB_PAGE = 30
STAMP = "%Y-%m-%dT%H:%M:%SZ"
ANTON = "anton"
MATRIX = re.compile(r" \(.*\)$")
TIERS = {"pull_request": "pull request", "pull_request_target": "pull request", "merge_group": "queue"}
TOP = 25
CACHE = Path("ci-minutes-jobs.json")
KEEP = timedelta(days=7)
# Calls left for the other users of the token's hourly limit after the job reads.
RESERVE = 1000


def tier(run: dict[str, Any]) -> str:
    if run["event"] in TIERS:
        return TIERS[run["event"]]
    if run["event"] == "push":
        return "promotion" if run["head_branch"] == "dev" else "branch push"
    return run["event"]


def job_name(name: str) -> str:
    return MATRIX.sub("", name)


def seconds(job: dict[str, Any]) -> float:
    if not job.get("started_at") or not job.get("completed_at"):
        return 0.0
    spent = (datetime.fromisoformat(job["completed_at"]) - datetime.fromisoformat(job["started_at"])).total_seconds()
    return max(spent, 0.0)


def on_anton(job: dict[str, Any]) -> bool:
    return any(label.startswith(ANTON) for label in job.get("labels") or ())


def aggregate(runs: list[dict[str, Any]], jobs: list[dict[str, Any]], merged: int) -> dict[str, Any]:
    by_run = {run["id"]: run for run in runs}
    tiers, workflows, names = (
        cast("defaultdict[str, float]", defaultdict(float)),
        cast("defaultdict[str, float]", defaultdict(float)),
        cast("defaultdict[tuple[str, str, str], float]", defaultdict(float)),
    )
    anton = hosted = 0.0
    for job in jobs:
        run = by_run.get(job["run_id"])
        if run is None:
            continue
        spent = seconds(job) / 60
        if not on_anton(job):
            hosted += spent
            continue
        anton += spent
        tiers[tier(run)] += spent
        workflows[run["path"]] += spent
        names[(tier(run), run["path"], job_name(job["name"]))] += spent

    def per(value: float) -> float | None:
        return round(value / merged, 1) if merged else None

    def ranked(table: Mapping[Any, float], key: Callable[[Any], dict[str, Any]]) -> list[dict[str, Any]]:
        ordered = sorted(table.items(), key=lambda item: -item[1])
        return [{**key(name), "minutes": round(spent, 1), "per_merged": per(spent)} for name, spent in ordered]

    return {
        "runs": len(runs),
        "jobs": len(jobs),
        "merged": merged,
        "anton_minutes": round(anton, 1),
        "hosted_minutes": round(hosted, 1),
        "anton_per_merged": per(anton),
        "tiers": ranked(tiers, lambda name: {"tier": name}),
        "workflows": ranked(workflows, lambda name: {"workflow": name}),
        "jobs_by_name": ranked(names, lambda name: {"tier": name[0], "workflow": name[1], "job": name[2]}),
    }


def render(result: dict[str, Any]) -> str:
    lines = [
        f"## Anton runner minutes {result['since']} to {result['until']}",
        "",
        f"{result['runs']} runs, {result['jobs']} jobs, {result['merged']} pull requests merged into dev.",
        f"Anton minutes {result['anton_minutes']}, per merged pull request {result['anton_per_merged']}; "
        f"hosted minutes {result['hosted_minutes']}.",
        "",
        "| Tier | Minutes | Per merged |",
        "| --- | ---: | ---: |",
        *(f"| {row['tier']} | {row['minutes']} | {row['per_merged']} |" for row in result["tiers"]),
        "",
        "| Workflow | Minutes | Per merged |",
        "| --- | ---: | ---: |",
        *(f"| {row['workflow']} | {row['minutes']} | {row['per_merged']} |" for row in result["workflows"]),
        "",
        "| Tier | Workflow | Job | Minutes | Per merged |",
        "| --- | --- | --- | ---: | ---: |",
        *(
            f"| {row['tier']} | {row['workflow']} | {row['job']} | {row['minutes']} | {row['per_merged']} |"
            for row in result["jobs_by_name"][:TOP]
        ),
        "",
    ]
    return "\n".join(lines)


def read_runs(repo: str, since: datetime, until: datetime) -> list[dict[str, Any]]:
    hours: list[tuple[datetime, datetime]] = []
    start = since
    while start < until:
        hours.append((start, min(start + HOUR, until) - timedelta(seconds=1)))
        start += HOUR

    def read(window: tuple[datetime, datetime]) -> list[dict[str, Any]]:
        first, last = (moment.strftime(STAMP) for moment in window)
        listed = rows(f"repos/{repo}/actions/runs?created={first}..{last}&per_page=100", "workflow_runs")
        if len(listed) >= LISTED_CAP:
            raise RuntimeError(f"{first}..{last} listed {len(listed)} runs, the most GitHub returns")
        return listed

    with ThreadPoolExecutor() as pool:
        found = [run for listed in pool.map(read, hours) for run in listed]
    return list({run["id"]: run for run in found}.values())


def read_jobs(repo: str, runs: list[dict[str, Any]], cache: dict[str, Any]) -> list[dict[str, Any]]:
    def read(run: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        return attempt(run), jobs_of(repo, run["id"])

    unread = [run for run in runs if attempt(run) not in cache]
    with ThreadPoolExecutor() as pool:
        fresh = dict(pool.map(read, unread))
    for run in unread:
        if run.get("status") == "completed":
            cache[attempt(run)] = {"created_at": run["created_at"], "jobs": fresh[attempt(run)]}
    return [job for key in map(attempt, runs) for job in (fresh[key] if key in fresh else cache[key]["jobs"])]


def jobs_of(repo: str, run_id: int) -> list[dict[str, Any]]:
    """GitHub answers 502 for a full page of a run whose jobs carry many steps, so that run is read in small pages."""
    path = f"repos/{repo}/actions/runs/{run_id}/jobs?filter=all&per_page="
    try:
        return rows(f"{path}{JOB_PAGE}", "jobs")
    except subprocess.CalledProcessError:
        return rows(f"{path}{SMALL_JOB_PAGE}", "jobs")


def attempt(run: dict[str, Any]) -> str:
    return f"{run['id']}:{run['run_attempt']}"


def load_cache(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(path.read_text())) if path.is_file() else {}


def save_cache(path: Path, cache: dict[str, Any], since: datetime) -> None:
    oldest = (since - KEEP).strftime(STAMP)
    path.write_text(json.dumps({key: entry for key, entry in cache.items() if entry["created_at"] >= oldest}))


def remaining() -> int:
    done = subprocess.run(["gh", "api", "rate_limit"], capture_output=True, check=True, timeout=30, text=True)
    return json.loads(done.stdout)["resources"]["core"]["remaining"]


def within_budget(reads: int) -> None:
    left = remaining()
    if reads + RESERVE > left:
        raise SystemExit(f"{reads} job reads would leave fewer than {RESERVE} of {left} API calls; refusing to start")


def merged_count(repo: str, since: datetime, until: datetime) -> int:
    query = f"repo:{repo}+is:pr+is:merged+base:dev+merged:{since.strftime(STAMP)}..{until.strftime(STAMP)}"
    return len(rows(f"search/issues?q={query}&per_page=100", "items"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--until")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--out", type=Path, default=Path("ci-minutes.json"))
    parser.add_argument("--cache", type=Path, default=CACHE)
    args = parser.parse_args(argv)
    until = datetime.strptime(args.until, STAMP) if args.until else datetime.now(UTC)
    since = until - timedelta(hours=args.hours)
    runs = read_runs(args.repo, since, until)
    cache = load_cache(args.cache)
    within_budget(sum(attempt(run) not in cache for run in runs))
    jobs = read_jobs(args.repo, runs, cache)
    save_cache(args.cache, cache, since)
    result = aggregate(runs, jobs, merged_count(args.repo, since, until))
    result.update(since=since.strftime(STAMP), until=until.strftime(STAMP))
    args.out.write_text(json.dumps(result, indent=2))
    report = render(result)
    print(report)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a") as handle:
            handle.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
