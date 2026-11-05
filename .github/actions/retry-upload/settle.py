"""Free the artifact name an earlier upload attempt still holds.

An attempt cut while its finalize request is in flight leaves the name held for good:
every create of that name answers 409 and the artifact is never listed. Before the next
attempt this asks the service, with the runtime token the runner gives actions, whether
the name is finalized or free; a held name is finalized as empty and deleted, so the
retry can create it again. It asks again until the name is usable or the seconds run out,
and never fails the step; the next attempt decides the outcome.
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SERVICE = "twirp/github.actions.results.api.v1.ArtifactService/"
INTERVAL = 10
TIMEOUT = 10
FREE = "free"
FINALIZED = "finalized"
CLEARED = "cleared"
HELD = "held"
UNKNOWN = "unknown"
USABLE = (FREE, FINALIZED, CLEARED)
CALLS = 4


def backend_ids(token: str) -> dict[str, str]:
    """Return the run and job backend ids the runtime token is scoped to."""
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    scope = next(item for item in claims["scp"].split() if item.startswith("Actions.Results:"))
    _, run, job = scope.split(":")
    return {"workflow_run_backend_id": run, "workflow_job_run_backend_id": job}


def call(base: str, token: str, method: str, body: dict[str, object]) -> tuple[int, dict]:
    """Post one artifact service request; a transport failure answers status 0."""
    request = urllib.request.Request(
        urllib.parse.urljoin(base, SERVICE + method),
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, {}
    except OSError:
        return 0, {}


def state(base: str, token: str, name: str) -> str:
    """Say whether the name is finalized, free to create, cleared of a held artifact, held, or unknown."""
    ids = backend_ids(token)
    status, listed = call(base, token, "ListArtifacts", {**ids, "name_filter": name})
    if status == 200 and listed.get("artifacts"):
        return FINALIZED
    status, _ = call(base, token, "CreateArtifact", {**ids, "name": name, "version": 4})
    if status != 409:
        return FREE if status == 200 else UNKNOWN
    finalized, _ = call(base, token, "FinalizeArtifact", {**ids, "name": name, "size": "0"})
    deleted, _ = call(base, token, "DeleteArtifact", {**ids, "name": name})
    return CLEARED if finalized == deleted == 200 else HELD


def settle(base: str, token: str, name: str, seconds: float) -> str:
    """Poll the name until it is finalized or free, or the seconds run out."""
    deadline = time.monotonic() + seconds
    while True:
        found = state(base, token, name)
        if found in USABLE or time.monotonic() + INTERVAL + CALLS * TIMEOUT > deadline:
            return found
        print(f"{name} is {found} at the artifact service; asking again in {INTERVAL}s.", flush=True)
        time.sleep(INTERVAL)


def main() -> int:
    """Settle the name in ARTIFACT within SETTLE_SECONDS and report the outcome."""
    name = os.environ["ARTIFACT"]
    token = os.environ.get("ACTIONS_RUNTIME_TOKEN", "")
    base = os.environ.get("ACTIONS_RESULTS_URL", "")
    if not token or not base:
        print(f"::warning title=Retried upload::No artifact service token; {name} is retried without settling.")
        return 0
    found = settle(base, token, name, float(os.environ["SETTLE_SECONDS"]))
    if found in USABLE:
        print(f"{name} is {found}; the next attempt can upload it.", flush=True)
    else:
        print(f"::warning title=Retried upload::{name} is still {found} after {os.environ['SETTLE_SECONDS']}s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
