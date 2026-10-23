import io
import json
import os
import re
import subprocess
import time
import zipfile
import zlib
from datetime import datetime, timezone

from scripts.swarm.lost_runner import home as _backoff_home
from scripts.swarm.store import SwarmError

RUN_URL = re.compile(r"https://github\.com/([^/]+/[^/]+)/actions/runs/([0-9]+)")
WORKFLOW = ".github/workflows/mutation-preflight.yml"
PROOFS = ".github/workflows/proofs.yml"
EVENTS = {WORKFLOW: "push", PROOFS: "workflow_dispatch"}
UNREADABLE = (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError)

BACKOFF = (2, 4)
JOB_FAILURES = ("failure", "timed_out", "cancelled")
SECONDARY_BACKOFF_S = 60


class Unreachable(ValueError):
    pass


class RateLimited(Unreachable):
    pass


class HeadUnreadable(SwarmError):
    pass


class Missing(ValueError):
    pass


class Skipped(Exception):
    pass


class FetchFailed(Exception):
    pass


def backing_off():
    return _held_until() > _now()


def rate_limited(text):
    return "rate limit" in str(text).lower()


def admitted_pending(target):
    _record({"at": _now(), "admitted_pending": target})


def _api(endpoint, binary=False):
    if backing_off():
        raise RateLimited(f"gh api {endpoint}: GitHub API rate limited until {_stamp(_held_until())}")
    try:
        done = subprocess.run(["gh", "api", endpoint], capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as error:
        raise Unreachable(f"gh api {endpoint}: {error}") from error
    if done.returncode:
        stderr = (done.stderr or b"").decode(errors="replace").strip()
        if rate_limited(stderr):
            until = hold(endpoint)
            raise RateLimited(f"gh api {endpoint}: GitHub API rate limited until {_stamp(until)}")
        raise Unreachable(f"gh api {endpoint}: {stderr or f'exited {done.returncode}'}")
    if binary:
        return done.stdout
    try:
        return json.loads(done.stdout)
    except ValueError as error:
        raise Unreachable(f"gh api {endpoint}: {error}") from error


def _now():
    return datetime.now().timestamp()


def _stamp(at):
    return datetime.fromtimestamp(at, timezone.utc).strftime("%H:%M:%S UTC")


def _held_until():
    try:
        until = json.loads((_backoff_home() / "github-backoff.json").read_text())["until"]
    except (OSError, ValueError, KeyError, TypeError):
        return 0
    return until if isinstance(until, (int, float)) else 0


def _core_reset(now):
    try:
        done = subprocess.run(["gh", "api", "rate_limit"], capture_output=True, timeout=20)
        core = json.loads(done.stdout)["resources"]["core"]
        if not done.returncode and core["remaining"] == 0 and core["reset"] > now:
            return core["reset"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        pass
    return now + SECONDARY_BACKOFF_S


def hold(endpoint):
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


def _record(entry):
    try:
        with (_backoff_home() / "github-backoff.jsonl").open("a") as log:
            log.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def read(target: str) -> dict | None:
    match = RUN_URL.fullmatch(target)
    if not match:
        return None
    repo, number = match.groups()
    try:
        return _api(f"repos/{repo}/actions/runs/{number}")
    except RateLimited:
        raise
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def bind(target: str) -> str | None:
    try:
        return _bind(target)
    except RateLimited:
        admitted_pending(target)
        return None


def _bind(target):
    return _bound(target, read(target))


def _bound(target, run):
    if not run or not _graded(run):
        raise SwarmError(
            "wait on mutation needs a readable branch Mutation preflight or focused mutation proof run url"
        )
    if not run.get("head_sha"):
        raise SwarmError("cannot read the mutation preflight head; retry the mutation wait")
    try:
        stale = _stale(target, run)
    except RateLimited:
        raise
    except UNREADABLE:
        raise HeadUnreadable("cannot read the branch head; retry the mutation wait") from None
    if stale:
        raise SwarmError("the mutation run grades a stale head; wait on a run for the branch head")
    return run["head_sha"]


def _graded(run):
    return run.get("path") in EVENTS and run.get("event") == EVENTS[run["path"]]


def _stale(target, run):
    repo = RUN_URL.fullmatch(target).group(1)
    return _api(f"repos/{repo}/git/ref/heads/{run['head_branch']}")["object"]["sha"] != run["head_sha"]


def _covers(target, run, scope, branches):
    if run["path"] != PROOFS:
        return True
    repo = RUN_URL.fullmatch(target).group(1)
    return scope == _scope(repo, "dev", run) or any(scope == _branch_scope(repo, ref, run) for ref in branches)


def _scope(repo, ref, run):
    base = _api(f"repos/{repo}/compare/{ref}...{run['head_sha']}")["merge_base_commit"]["sha"]
    return {"base": base, "head": run["head_sha"]}


def _branch_scope(repo, ref, run):
    try:
        return _scope(repo, ref, run)
    except RateLimited:
        raise
    except Unreachable as error:
        if "HTTP 404" not in str(error):
            raise
        return None


def _report(target, started_at):
    repo, number = RUN_URL.fullmatch(target).groups()
    since = datetime.fromisoformat(started_at)
    artifacts = _api(f"repos/{repo}/actions/runs/{number}/artifacts?per_page=100")
    matching = [
        artifact
        for artifact in artifacts["artifacts"]
        if artifact["name"] == "mutation-preflight-report"
        and not artifact["expired"]
        and datetime.fromisoformat(artifact["created_at"]) >= since
    ]
    if not matching:
        raise Missing
    artifact = max(matching, key=lambda row: row["id"])
    archive = _api(f"repos/{repo}/actions/artifacts/{artifact['id']}/zip", binary=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        name = next(name for name in zipped.namelist() if name == "report.json" or name.endswith("/report.json"))
        scope = json.loads(zipped.read("scope.json")) if "scope.json" in zipped.namelist() else None
        return json.loads(zipped.read(name)), scope


def _skip(target, run):
    if run["path"] != WORKFLOW or run["conclusion"] != "success":
        return
    repo, number = RUN_URL.fullmatch(target).groups()
    jobs = _api(f"repos/{repo}/actions/runs/{number}/jobs?per_page=100")["jobs"]
    delegated = any(
        job["name"] in ("pull-request", "plan")
        and job["conclusion"] == "success"
        and any(
            step["name"] == "Record the revisions the report grades" and step["conclusion"] == "skipped"
            for step in job["steps"]
        )
        for job in jobs
    )
    if not delegated:
        return
    runs = _api(
        f"repos/{repo}/actions/workflows/test.yml/runs?event=pull_request&head_sha={run['head_sha']}&per_page=100"
    )["workflow_runs"]
    for candidate in runs:
        if candidate["head_sha"] == run["head_sha"] and any(
            pr["base"]["ref"] == "dev" for pr in candidate["pull_requests"]
        ):
            raise Skipped(candidate["html_url"])
    raise Unreachable


def _fetch(target, run):
    for pause in (*BACKOFF, None):
        try:
            return _report(target, run["run_started_at"])
        except RateLimited:
            raise
        except (Unreachable, zipfile.BadZipFile, zlib.error, EOFError) as error:
            if pause is None:
                raise FetchFailed(error) from error
        except Missing:
            if pause == BACKOFF[0]:
                _skip(target, run)
            if pause is None:
                raise
        time.sleep(pause)


def _unfetched(outcome, conclusion, error):
    if isinstance(error, Skipped):
        return f"{outcome}, now skipped; the open pull request runs mutation in {error}"
    if conclusion == "failure":
        return f"{outcome}, now red; run failure; its mutation report could not be fetched: {error}"
    return (
        f"{outcome}, now unknown; the run concluded success but its mutation report could not be fetched: "
        f"{error}; wait on the run again"
    )


def resolution(held: dict, branches: list[str]) -> str:
    try:
        if not held.get("head"):
            run = read(held["target"])
            if run is None:
                return ""
            try:
                held["head"] = _bound(held["target"], run)
            except HeadUnreadable:
                return ""
            except SwarmError as error:
                return f"mutation preflight {held['target']}, now red; {error}"
        return _resolve(held, branches)
    except RateLimited:
        return ""


def _moved(target, run, outcome):
    try:
        stale = _stale(target, run)
    except RateLimited:
        raise
    except UNREADABLE:
        return f"{outcome}, now red; the branch head is unreadable"
    return f"{outcome}, now red; the branch moved past the graded head" if stale else ""


def _resolve(held, branches):
    target = held["target"]
    run = read(target)
    if run is None:
        return ""
    if not _graded(run) or run.get("head_sha") != held["head"]:
        return f"mutation preflight {target}, now red; run no longer matches the declared preflight head"
    if run.get("status") != "completed":
        return ""
    outcome = f"mutation preflight {target}"
    if moved := _moved(target, run, outcome):
        return moved
    if run.get("conclusion") != "success" and run.get("conclusion") != "failure":
        return f"{outcome}, now red; run {run.get('conclusion')}"
    try:
        report, scope = _fetch(target, run)
        covered = _covers(target, run, scope, branches)
        failures = [
            f"{file['path']}:{failure['name']}:{failure['fingerprint']} ({failure['status']})"
            for file in report["files"]
            for failure in file["failures"]
        ]
        failures.extend(f"{row['path']}: {row['reason']}" for row in report["not_mutated"])
        failed = report["failed"]
    except (Skipped, FetchFailed) as error:
        return _unfetched(outcome, run["conclusion"], error)
    except Unreachable:
        return ""
    except (*UNREADABLE, StopIteration, zipfile.BadZipFile):
        return f"{outcome}, now red; complete mutation report unavailable; mutation may have been skipped"
    if not covered:
        return f"{outcome}, now red; the mutation report does not grade the branch head from its dev merge base"
    if run["conclusion"] == "success" and not failed and not failures:
        return f"{outcome}, now green; no failing mutants"
    detail = "; ".join(failures) or "mutation failed without a named survivor"
    return f"{outcome}, now red; {detail}"


def run_resolution(target: str) -> str:
    repo, number = RUN_URL.fullmatch(target).groups()
    outcome = f"checks on {target}"
    try:
        run = _api(f"repos/{repo}/actions/runs/{number}")
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


def _failed_jobs(repo, number):
    try:
        jobs = _api(f"repos/{repo}/actions/runs/{number}/jobs?per_page=100")["jobs"]
        failed = [f"{job['name']} {job['conclusion']}" for job in jobs if job.get("conclusion") in JOB_FAILURES]
    except RateLimited:
        raise
    except (Unreachable, KeyError, TypeError, AttributeError):
        return "; its jobs could not be read"
    return f"; failed jobs: {', '.join(failed)}" if failed else ""
