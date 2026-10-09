import io
import json
import re
import subprocess
import zipfile

from scripts.swarm.store import SwarmError

RUN_URL = re.compile(r"https://github\.com/([^/]+/[^/]+)/actions/runs/([0-9]+)")
WORKFLOW = ".github/workflows/mutation-preflight.yml"


def _api(endpoint, binary=False):
    done = subprocess.run(["gh", "api", endpoint], capture_output=True, text=not binary, timeout=20)
    if done.returncode:
        raise ValueError("GitHub could not read the mutation preflight")
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
    if not run or run.get("path") != WORKFLOW or run.get("event") != "push":
        raise SwarmError("wait on mutation needs a readable branch Mutation preflight run url")
    if not run.get("head_sha"):
        raise SwarmError("cannot read the mutation preflight head; retry the mutation wait")
    return run["head_sha"]


def _report(target):
    repo, number = RUN_URL.fullmatch(target).groups()
    artifacts = _api(f"repos/{repo}/actions/runs/{number}/artifacts?per_page=100")
    matching = [
        artifact
        for artifact in artifacts["artifacts"]
        if artifact["name"] == "mutation-preflight-report" and not artifact["expired"]
    ]
    artifact = max(matching, key=lambda row: row["id"])
    archive = _api(f"repos/{repo}/actions/artifacts/{artifact['id']}/zip", binary=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        name = next(name for name in zipped.namelist() if name == "report.json" or name.endswith("/report.json"))
        return json.loads(zipped.read(name))


def resolution(held: dict) -> str:
    target = held["target"]
    run = read(target)
    if run is None:
        return ""
    if run.get("path") != WORKFLOW or run.get("event") != "push" or run.get("head_sha") != held["head"]:
        return f"mutation preflight {target}, now red; run no longer matches the declared preflight head"
    if run.get("status") != "completed":
        return ""
    outcome = f"mutation preflight {target}"
    if run.get("conclusion") != "success" and run.get("conclusion") != "failure":
        return f"{outcome}, now red; run {run.get('conclusion')}"
    try:
        report = _report(target)
        failures = [
            f"{file['path']}:{failure['name']}:{failure['fingerprint']} ({failure['status']})"
            for file in report["files"]
            for failure in file["failures"]
        ]
        failures.extend(f"{row['path']}: {row['reason']}" for row in report["not_mutated"])
        failed = report["failed"]
    except (
        OSError,
        subprocess.SubprocessError,
        ValueError,
        KeyError,
        TypeError,
        StopIteration,
        zipfile.BadZipFile,
    ):
        return f"{outcome}, now red; complete mutation report unavailable; mutation may have been skipped"
    if run["conclusion"] == "success" and not failed and not failures:
        return f"{outcome}, now green; no failing mutants"
    detail = "; ".join(failures) or "mutation failed without a named survivor"
    return f"{outcome}, now red; {detail}"
