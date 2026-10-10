import io
import json
import re
import subprocess
import zipfile
from datetime import datetime

from scripts.swarm.store import SwarmError

RUN_URL = re.compile(r"https://github\.com/([^/]+/[^/]+)/actions/runs/([0-9]+)")
WORKFLOW = ".github/workflows/mutation-preflight.yml"
PROOFS = ".github/workflows/proofs.yml"
EVENTS = {WORKFLOW: "push", PROOFS: "workflow_dispatch"}
UNREADABLE = (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError)


def _api(endpoint, binary=False):
    done = subprocess.run(["gh", "api", endpoint], capture_output=True, timeout=20)
    if done.returncode:
        raise ValueError
    return done.stdout if binary else json.loads(done.stdout)


def read(target: str) -> dict | None:
    match = RUN_URL.fullmatch(target)
    if not match:
        return None
    repo, number = match.groups()
    try:
        return _api(f"repos/{repo}/actions/runs/{number}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def bind(target: str) -> str:
    run = read(target)
    if not run or not _graded(run):
        raise SwarmError(
            "wait on mutation needs a readable branch Mutation preflight or focused mutation proof run url"
        )
    if not run.get("head_sha"):
        raise SwarmError("cannot read the mutation preflight head; retry the mutation wait")
    try:
        stale = _stale(target, run)
    except UNREADABLE:
        raise SwarmError("cannot read the branch head; retry the mutation wait") from None
    if stale:
        raise SwarmError("the mutation run grades a stale head; wait on a run for the branch head")
    return run["head_sha"]


def _graded(run):
    return run.get("path") in EVENTS and run.get("event") == EVENTS[run["path"]]


def _stale(target, run):
    repo = RUN_URL.fullmatch(target).group(1)
    return _api(f"repos/{repo}/git/ref/heads/{run['head_branch']}")["object"]["sha"] != run["head_sha"]


def _covers(target, run, scope):
    if run["path"] != PROOFS:
        return True
    repo = RUN_URL.fullmatch(target).group(1)
    base = _api(f"repos/{repo}/compare/dev...{run['head_sha']}")["merge_base_commit"]["sha"]
    return scope == {"base": base, "head": run["head_sha"]}


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
    artifact = max(matching, key=lambda row: row["id"])
    archive = _api(f"repos/{repo}/actions/artifacts/{artifact['id']}/zip", binary=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        name = next(name for name in zipped.namelist() if name == "report.json" or name.endswith("/report.json"))
        scope = json.loads(zipped.read("scope.json")) if "scope.json" in zipped.namelist() else None
        return json.loads(zipped.read(name)), scope


def resolution(held: dict) -> str:
    target = held["target"]
    run = read(target)
    if run is None:
        return ""
    if not _graded(run) or run.get("head_sha") != held["head"]:
        return f"mutation preflight {target}, now red; run no longer matches the declared preflight head"
    if run.get("status") != "completed":
        return ""
    outcome = f"mutation preflight {target}"
    try:
        if _stale(target, run):
            return f"{outcome}, now red; the branch moved past the graded head"
    except UNREADABLE:
        return f"{outcome}, now red; the branch head is unreadable"
    if run.get("conclusion") != "success" and run.get("conclusion") != "failure":
        return f"{outcome}, now red; run {run.get('conclusion')}"
    try:
        report, scope = _report(target, run["run_started_at"])
        covered = _covers(target, run, scope)
        failures = [
            f"{file['path']}:{failure['name']}:{failure['fingerprint']} ({failure['status']})"
            for file in report["files"]
            for failure in file["failures"]
        ]
        failures.extend(f"{row['path']}: {row['reason']}" for row in report["not_mutated"])
        failed = report["failed"]
    except (*UNREADABLE, StopIteration, zipfile.BadZipFile):
        return f"{outcome}, now red; complete mutation report unavailable; mutation may have been skipped"
    if not covered:
        return f"{outcome}, now red; the mutation report does not grade the branch head from its dev merge base"
    if run["conclusion"] == "success" and not failed and not failures:
        return f"{outcome}, now green; no failing mutants"
    detail = "; ".join(failures) or "mutation failed without a named survivor"
    return f"{outcome}, now red; {detail}"
