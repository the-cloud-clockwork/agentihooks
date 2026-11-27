"""The lab-verdict job: the base branch's lab authority and the lab App's verdict on each authoritative gate.

`authority` reads the gates the lab grades from the protected grader's delivery.yaml, so a pull request head can
never widen them, and AGENTIHOOKS_LAB_OFF empties them: lab.authoritative on pull requests, lab.queue.authoritative on
merge groups and nothing on any other event. `verdict` waits for the head's completed `lab/<gate>` check run from the
lab App named by AGENTIHOOKS_LAB_APP and publishes `lab_<gate>` for each authoritative gate: success or skipped as
posted, so the lab marks a gate this pull request does not run as skipped, and failure for any other conclusion or a
verdict absent or pending at the deadline.
"""

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import scripts.ci_budget.rate_limit
from scripts.ci_lab.delivery import OUTPUT, cutover, lab, queue_cutover

gh_api = cast("Callable[..., subprocess.CompletedProcess[str]]", scripts.ci_budget.rate_limit.gh_api)

PULL_REQUEST = "pull_request"
MERGE_GROUP = "merge_group"
COMPLETED = "completed"
PASSED = ("success", "skipped")
FAILURE = "failure"
POLL_S = 15.0
WAIT_S = 1200.0
TIMEOUT_S = 30.0

Fetch = Callable[[str], list[dict[str, Any]]]


def authority(grader_root: Path, event: str, environ: Mapping[str, str]) -> dict[str, str]:
    """Return the authoritative output: the base's gates as a JSON object on pull requests and merge groups, nothing
    elsewhere."""
    readers = {PULL_REQUEST: cutover, MERGE_GROUP: queue_cutover}
    if event not in readers:
        return {}
    gates = readers[event](grader_root, environ)
    return {"authoritative": json.dumps({gate: True for gate in gates}, sort_keys=True)}


def conclusion(runs: Sequence[Mapping[str, Any]], app: str) -> str | None:
    """Return the newest run of the trusted App: success or skipped as posted, any other conclusion as failure.

    None while no run of that App exists or the newest one has not completed.
    """
    trusted = [run for run in runs if run["app"]["slug"] == app]
    if not trusted:
        return None
    newest = max(trusted, key=lambda run: run["id"])
    if newest["status"] != COMPLETED:
        return None
    return newest["conclusion"] if newest["conclusion"] in PASSED else FAILURE


def lost(listings: Mapping[str, Sequence[Mapping[str, Any]]], app: str) -> list[str]:
    """Return the checks whose newest run of the trusted App completed neutral; none is named yet."""
    del listings, app
    return []


def verdicts(
    checks: Mapping[str, str],
    fetch: Fetch,
    app: str,
    wait: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, str]:
    """Poll each gate's check run until every verdict is complete or the wait ends; a missing verdict is a failure."""
    deadline = clock() + wait
    results: dict[str, str] = {}
    while True:
        for gate, check in checks.items():
            if gate not in results:
                found = conclusion(fetch(check), app)
                if found is not None:
                    results[gate] = found
        if len(results) == len(checks) or clock() >= deadline:
            return {gate: results.get(gate, FAILURE) for gate in checks}
        sleep(POLL_S)


def fetcher(repo: str, sha: str) -> Fetch:
    """Return a reader of one head's latest check runs by name through the rate limited GitHub API helper."""

    def fetch(check: str) -> list[dict[str, Any]]:
        """Return the latest check runs named check on the head."""
        done = gh_api(
            [f"repos/{repo}/commits/{sha}/check-runs?check_name={quote(check)}&filter=latest&per_page=100"],
            timeout=TIMEOUT_S,
            text=True,
        )
        return json.loads(done.stdout)["check_runs"]

    return fetch


def lab_outputs(grader_root: Path, authoritative: str, fetch: Fetch, app: str, wait: float) -> dict[str, str]:
    """Return lab_<gate> for every authoritative gate, named by the base's check prefix."""
    gates = sorted(gate for gate, held in json.loads(authoritative or "{}").items() if held is True)
    prefix = lab(grader_root)["check_prefix"]
    results = verdicts({gate: f"{prefix}/{gate}" for gate in gates}, fetch, app, wait)
    return {f"lab_{gate}": result for gate, result in results.items()}


def publish(outputs: Mapping[str, str], environ: Mapping[str, str]) -> None:
    """Append each output to the step output file and print it."""
    lines = "".join(f"{key}={value}\n" for key, value in outputs.items())
    with Path(environ[OUTPUT]).open("a") as handle:
        handle.write(lines)
    print(lines, end="")


def main(argv: Sequence[str], environ: Mapping[str, str] = os.environ) -> int:
    """Run `authority <grader root> <event>` or `verdict <grader root> <authoritative> <repo> <sha> <app>`."""
    step, grader, *rest = argv
    if step == "authority":
        [event] = rest
        outputs = authority(Path(grader), event, environ)
    elif step == "verdict":
        authoritative, repo, sha, app = rest
        if not app:
            raise ValueError("no lab App is named; set AGENTIHOOKS_LAB_APP to the lab App slug")
        outputs = lab_outputs(Path(grader), authoritative, fetcher(repo, sha), app, WAIT_S)
    else:
        raise ValueError(f"unknown step {step!r}; expected authority or verdict")
    publish(outputs, environ)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
