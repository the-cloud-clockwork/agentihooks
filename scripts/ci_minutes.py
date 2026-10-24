"""Measures Anton runner minutes per merged pull request, split by tier, workflow and job, from Actions job timings.

GitHub lists at most 1000 runs for one filtered query, so runs are read one hour at a time.
"""

import argparse
import json
import os
import re
import subprocess
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

ATTEMPTS = 3
WORKERS = 8
HOUR = timedelta(hours=1)
STAMP = "%Y-%m-%dT%H:%M:%SZ"
ANTON = "anton"
MATRIX = re.compile(r" \(.*\)$")
TIERS = {"pull_request": "pull request", "pull_request_target": "pull request", "merge_group": "queue"}
TOP = 25


def tier(run: dict) -> str:
    if run["event"] in TIERS:
        return TIERS[run["event"]]
    if run["event"] == "push":
        return "promotion" if run["head_branch"] == "dev" else "branch push"
    return run["event"]


def job_name(name: str) -> str:
    return MATRIX.sub("", name)


def seconds(job: dict) -> float:
    if not job.get("started_at") or not job.get("completed_at"):
        return 0.0
    began = datetime.fromisoformat(job["started_at"])
    ended = datetime.fromisoformat(job["completed_at"])
    return max(0.0, (ended - began).total_seconds())


def on_anton(job: dict) -> bool:
    return any(label.startswith(ANTON) for label in job.get("labels") or ())


def aggregate(runs: list[dict], jobs: list[dict], merged: int) -> dict:
    by_run = {run["id"]: run for run in runs}
    tiers, workflows, names = defaultdict(float), defaultdict(float), defaultdict(float)
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

    def per(value):
        return round(value / merged, 1) if merged else None

    def ranked(table, key):
        rows = sorted(table.items(), key=lambda item: -item[1])
        return [{**key(name), "minutes": round(spent, 1), "per_merged": per(spent)} for name, spent in rows]

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


def render(result: dict, since: str, until: str) -> str:
    lines = [
        f"## Anton runner minutes {since} to {until}",
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


def _gh(path: str, jq: str) -> list[dict]:
    for attempt in range(ATTEMPTS):
        try:
            done = subprocess.run(
                ["gh", "api", "--paginate", path, "--jq", jq], capture_output=True, text=True, check=True, timeout=300
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if attempt == ATTEMPTS - 1:
                raise
            time.sleep(attempt + 1)
        else:
            return [json.loads(line) for line in done.stdout.splitlines() if line]


def read_runs(repo: str, since: datetime, until: datetime) -> list[dict]:
    hours = []
    start = since
    while start < until:
        hours.append((start, min(start + HOUR, until) - timedelta(seconds=1)))
        start += HOUR
    jq = ".workflow_runs[] | {id, path, event, head_branch}"

    def read(window):
        first, last = (moment.strftime(STAMP) for moment in window)
        return _gh(f"repos/{repo}/actions/runs?created={first}..{last}&per_page=100", jq)

    with ThreadPoolExecutor(WORKERS) as pool:
        found = [run for rows in pool.map(read, hours) for run in rows]
    return list({run["id"]: run for run in found}.values())


def read_jobs(repo: str, runs: list[dict]) -> list[dict]:
    jq = ".jobs[] | {run_id, name, labels, started_at, completed_at}"

    def read(run):
        return _gh(f"repos/{repo}/actions/runs/{run['id']}/jobs?filter=all&per_page=100", jq)

    with ThreadPoolExecutor(WORKERS) as pool:
        return [job for rows in pool.map(read, runs) for job in rows]


def merged_count(repo: str, since: datetime, until: datetime) -> int:
    window = f"{since.strftime(STAMP)}..{until.strftime(STAMP)}"
    query = f"repo:{repo}+is:pr+is:merged+base:dev+merged:{window}"
    done = subprocess.run(
        ["gh", "api", f"search/issues?q={query}&per_page=1", "--jq", ".total_count"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return int(done.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--until", help="UTC end of the window, default now")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--out", type=Path, default=Path("ci-minutes.json"))
    args = parser.parse_args(argv)
    until = datetime.strptime(args.until, STAMP).replace(tzinfo=UTC) if args.until else datetime.now(UTC)
    until = until.replace(microsecond=0)
    since = until - timedelta(hours=args.hours)
    runs = read_runs(args.repo, since, until)
    result = aggregate(runs, read_jobs(args.repo, runs), merged_count(args.repo, since, until))
    result.update(since=since.strftime(STAMP), until=until.strftime(STAMP))
    args.out.write_text(json.dumps(result, indent=2))
    report = render(result, result["since"], result["until"])
    print(report)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a") as handle:
            handle.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
