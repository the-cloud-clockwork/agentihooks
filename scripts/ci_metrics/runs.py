"""Reads one hour of agentihooks CI from GitHub and the ARC pending runner average from Prometheus.

Every agent shares one GitHub API limit, so a run reads one hour window: its workflow runs, the jobs of each run with
their steps, the pull requests merged into dev in the window and the runs of each merged branch. Job reads are refused
when they would leave less than the reserve that `scripts.ci_minutes` keeps for the other users of the limit.
"""

import json
import subprocess
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from scripts import ci_minutes
from scripts.ci_storage import rows

PULL_PAGES = 10
TIMEOUT_S = 60
PENDING = "avg_over_time(sum by (name) (gha_controller_pending_ephemeral_runners)[1h:1m])"


@dataclass(frozen=True)
class Pull:
    """What one window's report reads: its runs and their jobs, its merged pull requests and their branch runs."""

    runs: list[dict[str, Any]]
    jobs: list[dict[str, Any]]
    merged: list[dict[str, Any]]
    branch_runs: list[dict[str, Any]]


def window(until: datetime) -> tuple[datetime, datetime]:
    """The one hour window that ends at `until`."""
    return until - ci_minutes.HOUR, until


def lagged(now: datetime, lag: timedelta) -> datetime:
    """The hour boundary `lag` before the hour `now` falls in, so the window's runs have finished."""
    return now.replace(minute=0, second=0, microsecond=0) - lag


def api(path: str) -> Any:
    """One GitHub API read through gh."""
    done = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=True, timeout=TIMEOUT_S)
    return json.loads(done.stdout)


def merged_pull(pull: dict[str, Any]) -> dict[str, Any]:
    """The fields of a pull request the report reads."""
    return {
        "number": pull["number"],
        "branch": pull["head"]["ref"],
        "created_at": pull["created_at"],
        "merged_at": pull["merged_at"],
    }


def merged_pulls(repo: str, since: datetime, until: datetime) -> list[dict[str, Any]]:
    """Pull requests merged into dev in the window.

    Closed pull requests are listed newest update first, and a merge updates its pull request, so paging stops at the
    first page that reaches an update older than the window. A run whose page cap ends before that refuses rather
    than count fewer merges.
    """
    found: list[dict[str, Any]] = []
    oldest = since.strftime(ci_minutes.STAMP)
    for page in range(1, PULL_PAGES + 1):
        listed = api(f"repos/{repo}/pulls?state=closed&base=dev&sort=updated&direction=desc&per_page=100&page={page}")
        found.extend(listed)
        if not listed or listed[-1]["updated_at"] < oldest:
            break
    else:
        raise SystemExit(f"ci metrics refused: {PULL_PAGES} pages of closed pull requests did not reach {oldest}")
    return [
        merged_pull(pull)
        for pull in found
        if pull["merged_at"] and since <= datetime.fromisoformat(pull["merged_at"]) < until
    ]


def branch_runs(repo: str, branch: str) -> list[dict[str, Any]]:
    """Every workflow run on one branch."""
    name = urllib.parse.quote(branch)
    return rows(f"repos/{repo}/actions/runs?branch={name}&per_page=100", "workflow_runs")


def pending(url: str, until: datetime) -> dict[str, float]:
    """Average pending ephemeral runners per ARC runner pool over the hour before `until`."""
    query = urllib.parse.urlencode({"query": PENDING, "time": until.timestamp()})
    with urllib.request.urlopen(f"{url}/api/v1/query?{query}", timeout=TIMEOUT_S) as reply:
        found = json.load(reply)
    return {item["metric"]["name"]: float(item["value"][1]) for item in found["data"]["result"]}


def read(repo: str, since: datetime, until: datetime) -> Pull:
    """Read one window, refusing before the job reads when they would spend the shared reserve."""
    runs = ci_minutes.read_runs(repo, since, until)
    merged = merged_pulls(repo, since, until)
    ci_minutes.within_budget(len(runs) + len(merged))
    jobs = ci_minutes.read_jobs(repo, runs, {})
    return Pull(runs, jobs, merged, [run for pull in merged for run in branch_runs(repo, pull["branch"])])
