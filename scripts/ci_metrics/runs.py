# pyright: reportUnusedImport=false
"""Reads one hour of agentihooks CI from GitHub and the ARC pending runner average from Prometheus.

Every agent shares one GitHub API limit, so a run reads one hour window: its workflow runs, the jobs of each run with
their steps, the pull requests merged into dev in the window and the runs of each merged branch. Job reads are refused
when they would leave less than the reserve that `scripts.ci_minutes` keeps for the other users of the limit.
"""

import json  # noqa: F401
import subprocess  # noqa: F401
import urllib.parse
import urllib.request  # noqa: F401
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from scripts import ci_minutes  # noqa: F401
from scripts.ci_storage import rows  # noqa: F401

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
    raise NotImplementedError()


def lagged(now: datetime, lag: timedelta) -> datetime:
    """The hour boundary `lag` before the hour `now` falls in, so the window's runs have finished."""
    raise NotImplementedError()


def api(path: str) -> Any:
    """One GitHub API read through gh."""
    raise NotImplementedError()


def merged_pull(pull: dict[str, Any]) -> dict[str, Any]:
    """The fields of a pull request the report reads."""
    raise NotImplementedError()


def merged_pulls(repo: str, since: datetime, until: datetime) -> list[dict[str, Any]]:
    """Pull requests merged into dev in the window.

    Closed pull requests are listed newest update first, and a merge updates its pull request, so paging stops at the
    first page that reaches an update older than the window."""
    raise NotImplementedError()


def branch_runs(repo: str, branch: str) -> list[dict[str, Any]]:
    """Every workflow run on one branch."""
    raise NotImplementedError()


def pending(url: str, until: datetime) -> dict[str, float]:
    """Average pending ephemeral runners per ARC runner pool over the hour before `until`."""
    raise NotImplementedError()


def read(repo: str, since: datetime, until: datetime) -> Pull:
    """Read one window, refusing before the job reads when they would spend the shared reserve."""
    raise NotImplementedError()
