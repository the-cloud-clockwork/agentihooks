"""Actions run waits: a checks wait on a run url ends with the run's conclusion, and every swarm GitHub read holds
behind one shared rate limit backoff."""

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Any

from scripts.swarm import github_reads, stack
from scripts.swarm.lost_runner import home as _backoff_home
from scripts.tick.deadline import Deadline

RUN_URL = re.compile(r"https://github\.com/([^/]+/[^/]+)/actions/runs/([0-9]+)")
JOB_FAILURES = ("failure", "timed_out", "cancelled")
SECONDARY_BACKOFF_S = 60
READ_SECONDS = 20


class Unreachable(ValueError):
    pass


class RateLimited(Unreachable):
    pass


def backing_off() -> bool:
    return _held_until() > _now()


def rate_limited(text: object) -> bool:
    return "rate limit" in str(text).lower()


def admitted_pending(target: str) -> None:
    _record({"at": _now(), "admitted_pending": target})


def _api(endpoint: str, caller: str) -> Any:
    if backing_off():
        raise RateLimited(f"gh api {endpoint}: GitHub API rate limited until {_stamp(_held_until())}")
    try:
        with github_reads.calling(caller):
            body = stack.login_source_control().download(endpoint, deadline=Deadline.after(READ_SECONDS))
    except subprocess.CalledProcessError as error:
        stderr = (error.stderr or b"").decode(errors="replace").strip()
        if rate_limited(stderr):
            until = hold(endpoint)
            raise RateLimited(f"gh api {endpoint}: GitHub API rate limited until {_stamp(until)}") from error
        raise Unreachable(f"gh api {endpoint}: {stderr or f'exited {error.returncode}'}") from error
    except (OSError, subprocess.SubprocessError) as error:
        raise Unreachable(f"gh api {endpoint}: {error}") from error
    try:
        return json.loads(body)
    except ValueError as error:
        raise Unreachable(f"gh api {endpoint}: {error}") from error


def _run(repo: str, number: str, caller: str) -> dict[str, Any]:
    return read(f"repos/{repo}/actions/runs/{number}", caller)


def read(endpoint: str, caller: str) -> dict[str, Any]:
    """One GitHub read of an Actions run or check run endpoint, through the shared run cache and backoff."""
    return github_reads.run(endpoint, _now(), lambda: _api(endpoint, caller))


def _now() -> float:
    return datetime.now().timestamp()


def _stamp(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).strftime("%H:%M:%S UTC")


def _held_until() -> float:
    try:
        until = json.loads((_backoff_home() / "github-backoff.json").read_text())["until"]
    except (OSError, ValueError, KeyError, TypeError):
        return 0
    return until if isinstance(until, (int, float)) else 0


def _core_reset(now: float) -> float:
    try:
        core = stack.login_source_control().rate_limit(deadline=Deadline.after(READ_SECONDS))
    except (*stack.PORT_ERRORS, TypeError):
        return now + SECONDARY_BACKOFF_S
    return core.reset if core.remaining == 0 and core.reset > now else now + SECONDARY_BACKOFF_S


def hold(endpoint: str) -> float:
    now = _now()
    until = max(_core_reset(now), _held_until())
    home = _backoff_home()
    try:
        home.mkdir(parents=True, exist_ok=True)
        staged = home / f"github-backoff.json.{os.getpid()}"
        staged.write_text(json.dumps({"until": until}))
        staged.replace(home / "github-backoff.json")
    except OSError:
        pass
    _record({"at": now, "endpoint": endpoint, "until": until})
    return until


def _record(entry: dict[str, object]) -> None:
    try:
        with (_backoff_home() / "github-backoff.jsonl").open("a") as log:
            log.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def run_resolution(target: str) -> str:
    repo, number = RUN_URL.findall(target)[0]
    outcome = f"checks on {target}"
    try:
        run = _run(repo, number, "waits.checks")
    except Unreachable as error:
        return f"{outcome}, missing; GitHub has no such run" if "HTTP 404" in str(error) else ""
    if run.get("status") != "completed":
        return ""
    conclusion = run.get("conclusion")
    if conclusion == "success":
        return f"{outcome}, now green"
    if conclusion == "cancelled":
        return f"{outcome}, cancelled; dispatch the run again and wait on the new one"
    try:
        return f"{outcome}, now red; the run concluded {conclusion}{_failed_jobs(repo, number)}"
    except RateLimited:
        return ""


def _failed_jobs(repo: str, number: str) -> str:
    try:
        jobs = _api(f"repos/{repo}/actions/runs/{number}/jobs?per_page=100", "waits.checks")["jobs"]
        failed = [f"{job['name']} {job['conclusion']}" for job in jobs if job.get("conclusion") in JOB_FAILURES]
    except RateLimited:
        raise
    except (Unreachable, KeyError, TypeError, AttributeError):
        return "; its jobs could not be read"
    return f"; failed jobs: {', '.join(failed)}" if failed else ""
