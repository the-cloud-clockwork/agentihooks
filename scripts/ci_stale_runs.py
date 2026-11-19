import json
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

from scripts.ci_budget import rate_limit

__all__ = ["time"]

gh_api = cast("Callable[..., subprocess.CompletedProcess[str]]", rate_limit.gh_api)


def _api(*args: str) -> str:
    return gh_api(list(args), timeout=120, text=True).stdout


def queued(repo: str) -> list[dict[str, Any]]:
    url = f"repos/{repo}/actions/runs?status=queued&per_page=100"
    return [json.loads(line) for line in _api("--paginate", url, "--jq", ".workflow_runs[]").splitlines()]


def stale(runs: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    raise NotImplementedError


def jobs(repo: str, run_id: int) -> str:
    raise NotImplementedError


def clear(repo: str, runs: list[dict[str, Any]]) -> list[int]:
    raise NotImplementedError


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError
