"""Copy a passed queue run's unit results into the twenty legs a reusing dev push uploads, in one job.

A dev push whose queue run passed on the same commit reuses that run's unit results. Its one unit
job restores every queue leg, lays out each push leg as a reusing unit leg would, checks and
summarizes each leg's coverage with the unit legs' own commands, and uploads every leg's durations,
coverage, flakes and executed lines in that order, legs side by side. The upload runs inside an
actions/github-script step, which hands it the job's runtime token for the results service
upload-artifact uses.
"""

import argparse
import base64
import hashlib
import http.client
import io
import json
import os
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Any

LEGS = 20
VERSION = "3.12"
KINDS = {"durations": 14, "coverage": 7, "flakes": 14, "executed-lines": 7}
SERVICE = "twirp/github.actions.results.api.v1.ArtifactService/"
OWNER = ("workflow_run_backend_id", "workflow_job_run_backend_id")
TIMEOUT = 120
ATTEMPTS = 3
WAIT = 10
AT_ONCE = 8
FAILURES = (OSError, ValueError, KeyError, RuntimeError, http.client.HTTPException)


def artifact(kind: str, leg: int) -> str:
    """Name the artifact a unit leg uploads for one kind of result."""
    return f"{kind}-{VERSION}-{leg}"


def layout(queue: Path, legs: dict[str, int], out: Path) -> None:
    """Lay out every push leg from the queue leg it maps onto, as a reusing unit leg leaves it before its uploads."""
    for leg in range(1, LEGS + 1):
        source = legs[str(leg)]
        durations = shutil.copytree(queue / artifact("durations", source), out / artifact("durations", leg))
        shutil.copytree(queue / artifact("coverage", source), out / artifact("coverage", leg), copy_function=os.link)
        flakes = out / artifact("flakes", leg)
        if source == leg:
            shutil.copytree(queue / artifact("flakes", leg), flakes)
        else:
            (durations / "durations.json").write_text("{}\n")
            (durations / "elapsed.json").unlink(missing_ok=True)
            flakes.mkdir()
        (flakes / "flakes.jsonl").touch()
        (out / artifact("executed-lines", leg)).mkdir()


def archive(folder: Path) -> bytes:
    """Zip every file under the folder by its path inside it, as upload-artifact does."""
    files = sorted(path for path in folder.rglob("*") if path.is_file())
    if not files:
        raise ValueError(f"{folder} holds no file to upload")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zipped:
        for path in files:
            zipped.write(path, path.relative_to(folder).as_posix())
    return buffer.getvalue()


def expiry(days: int, now: datetime) -> str:
    """The expiry upload-artifact sets for the retention days, held to the repository's longest retention."""
    kept = min(days, int(os.environ.get("GITHUB_RETENTION_DAYS") or days))
    return (now + timedelta(days=kept)).strftime("%Y-%m-%dT%H:%M:%SZ")


def backend_ids(token: str) -> dict[str, str]:
    """Return the run and job backend ids the runtime token is scoped to."""
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    scope = next(item for item in claims["scp"].split() if item.startswith("Actions.Results:"))
    _, run, job = scope.split(":")
    return {"workflow_run_backend_id": run, "workflow_job_run_backend_id": job}


class Results:
    """The results service upload-artifact reaches with the job's runtime token."""

    def __init__(self, base: str, token: str) -> None:
        """Keep the service address, the token and the backend ids every request names."""
        self.base = base
        self.token = token
        self.ids = backend_ids(token)

    def call(self, method: str, body: dict[str, object]) -> dict[str, Any]:
        """Post one request for this job and return the service's answer; an answer other than 200 raises."""
        request = urllib.request.Request(
            urllib.parse.urljoin(self.base, SERVICE + method),
            data=json.dumps({**self.ids, **body}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read() or b"{}")

    def create(self, name: str, days: int) -> str:
        """Create the artifact, freeing a name an earlier attempt left unfinalized, and return its upload address."""
        request: dict[str, object] = {"name": name, "version": 4, "expires_at": expiry(days, datetime.now(UTC))}
        try:
            created = self.call("CreateArtifact", request)
        except urllib.error.HTTPError as error:
            if error.code != HTTPStatus.CONFLICT:
                raise
            self.call("FinalizeArtifact", {"name": name, "size": "0"})
            self.call("DeleteArtifact", {"name": name})
            created = self.call("CreateArtifact", request)
        if not created.get("ok"):
            raise RuntimeError(f"the results service did not create {name}")
        return created["signed_upload_url"]

    def upload(self, name: str, folder: Path, days: int) -> None:
        """Upload the folder's files as one artifact, replacing any artifact of that name the run holds."""
        data = archive(folder)
        listed: list[dict[str, Any]] = self.call("ListArtifacts", {"name_filter": name}).get("artifacts") or []
        for held in listed:
            self.call("DeleteArtifact", {**{key: str(held[key]) for key in OWNER}, "name": name})
        blob = urllib.request.Request(
            self.create(name, days),
            data=data,
            headers={"x-ms-blob-type": "BlockBlob", "Content-Type": "zip"},
            method="PUT",
        )
        with urllib.request.urlopen(blob, timeout=TIMEOUT):
            pass
        digest = hashlib.sha256(data).hexdigest()
        finalized = self.call("FinalizeArtifact", {"name": name, "size": str(len(data)), "hash": f"sha256:{digest}"})
        if not finalized.get("ok"):
            raise RuntimeError(f"the results service did not finalize {name}")


def retried(results: Results, name: str, folder: Path, days: int, pause: Callable[[float], None]) -> None:
    """Upload one artifact, retrying in place up to the attempt limit."""
    for attempt in range(1, ATTEMPTS):
        try:
            results.upload(name, folder, days)
            return
        except FAILURES as error:
            print(f"::warning title=Retried upload::Attempt {attempt} to upload {name} failed: {error}; retrying.")
            pause(WAIT)
    results.upload(name, folder, days)


def upload(out: Path, results: Results, pause: Callable[[float], None] = time.sleep) -> None:
    """Upload every push leg's artifacts, each leg in the unit job's order, several legs at once."""

    def leg(number: int) -> None:
        for kind, days in KINDS.items():
            retried(results, artifact(kind, number), out / artifact(kind, number), days, pause)

    with ThreadPoolExecutor(max_workers=AT_ONCE) as pool:
        list(pool.map(leg, range(1, LEGS + 1)))


def main(argv: list[str] | None = None) -> int:
    """Lay out the push legs from the restored queue legs, or upload the laid out legs."""
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    laying = commands.add_parser("layout")
    laying.add_argument("--queue", type=Path, required=True)
    laying.add_argument("--legs", required=True)
    laying.add_argument("--out", type=Path, required=True)
    sending = commands.add_parser("upload")
    sending.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "layout":
        layout(args.queue, json.loads(args.legs), args.out)
    else:
        upload(args.out, Results(os.environ["ACTIONS_RESULTS_URL"], os.environ["ACTIONS_RUNTIME_TOKEN"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
