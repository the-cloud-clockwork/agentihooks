"""Download every artifact of this run whose name matches a pattern into one folder.

It runs inside an actions/github-script step, which hands it the runtime token the runner gives
actions, and asks the results service download-artifact uses, so it spends no REST quota. Every
answer is closed whatever its status before the next try: download-artifact 8.0.2 leaves a
non-200 blob answer unread, and that answer's open socket keeps its step alive for three minutes
after the download finished. Every try ends by one deadline shorter than the step's one minute
timer, and a watchdog ends the process soon after it whatever socket operation still blocks, so a
part that never arrives is named instead of cut off by the timer.
"""

import base64
import hashlib
import http.client
import io
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

SERVICE = "twirp/github.actions.results.api.v1.ArtifactService/"
OWNER = ("workflow_run_backend_id", "workflow_job_run_backend_id")
TIMEOUT = 15
DEADLINE = 45.0
GRACE = 5.0
CHUNK = 1 << 20
TRIES = 3
PAUSE = 2.0
PARALLEL = 5
LISTING = "this run's artifact listing"


@dataclass(frozen=True)
class Artifact:
    """A listed artifact: its name, the backend ids of the job that uploaded it and its recorded digest."""

    name: str
    owner: dict[str, str]
    digest: str


def backend_ids(token: str) -> dict[str, str]:
    """Return the run and job backend ids the runtime token is scoped to."""
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    scope = next(item for item in claims["scp"].split() if item.startswith("Actions.Results:"))
    _, run, job = scope.split(":")
    return dict(zip(OWNER, (run, job), strict=True))


def read(request: urllib.request.Request | str, deadline: float) -> bytes:
    """Return the body of a 200 answer read before the deadline; any other answer is closed and raised as an OSError."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("out of time")
    try:
        with urllib.request.urlopen(request, timeout=min(TIMEOUT, remaining)) as answer:
            if answer.status != 200:
                raise OSError(f"answered {answer.status}")
            body = bytearray()
            while chunk := answer.read1(CHUNK):
                body += chunk
                if time.monotonic() > deadline:
                    raise TimeoutError("out of time")
            return bytes(body)
    except urllib.error.HTTPError as error:
        error.close()
        raise


class Results:
    """The results service, reached with the job's runtime token."""

    def __init__(self, base: str, token: str, deadline: float) -> None:
        """Keep the service address, the token, the backend ids every request names and the moment every try must end by."""
        self.base = base
        self.token = token
        self.ids = backend_ids(token)
        self.deadline = deadline

    def call(self, method: str, body: dict[str, str]) -> dict[str, Any]:
        """Post one request and return its JSON answer."""
        request = urllib.request.Request(
            urllib.parse.urljoin(self.base, SERVICE + method),
            data=json.dumps({**self.ids, **body}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
            method="POST",
        )
        answer: dict[str, Any] = json.loads(read(request, self.deadline) or b"{}")
        return answer

    def listed(self, pattern: str) -> list[Artifact]:
        """Return the newest artifact of each name the pattern matches, in name order."""
        newest: dict[str, tuple[int, Artifact]] = {}
        items: list[dict[str, Any]] = self.call("ListArtifacts", {}).get("artifacts") or []
        for item in items:
            name, ident = str(item["name"]), int(item["database_id"])
            if fnmatchcase(name, pattern) and (name not in newest or ident >= newest[name][0]):
                owner = {key: str(item[key]) for key in OWNER}
                newest[name] = (ident, Artifact(name, owner, str(item.get("digest") or "")))
        return [newest[name][1] for name in sorted(newest)]

    def archive(self, artifact: Artifact) -> zipfile.ZipFile:
        """Fetch the artifact's blob, signed for the job that uploaded it, and check it against its digest."""
        url = self.call("GetSignedArtifactURL", {**artifact.owner, "name": artifact.name}).get("signed_url")
        if not isinstance(url, str) or not url:
            raise OSError("no signed url")
        blob = read(url, self.deadline)
        if artifact.digest and artifact.digest != f"sha256:{hashlib.sha256(blob).hexdigest()}":
            raise OSError("digest mismatch")
        return zipfile.ZipFile(io.BytesIO(blob))


def retried[T](work: Callable[[], T], what: str, sleep: Callable[[float], None], deadline: float) -> T | None:
    """Return what work returns, trying again after a pause when an answer fails; None once the tries or the time run out."""
    for attempt in range(1, TRIES + 1):
        try:
            return work()
        except (OSError, ValueError, KeyError, http.client.HTTPException, zipfile.BadZipFile) as error:
            print(f"::warning::Downloading {what} failed on try {attempt} of {TRIES}: {error}", flush=True)
            if attempt == TRIES or time.monotonic() + PAUSE >= deadline:
                break
            sleep(PAUSE)
    return None


def download(results: Results, pattern: str, folder: Path, sleep: Callable[[float], None] = time.sleep) -> list[str]:
    """Extract every newest artifact matching the pattern into the folder; return what never arrived."""
    artifacts = retried(lambda: results.listed(pattern), LISTING, sleep, results.deadline)
    if artifacts is None:
        return [LISTING]

    def take(artifact: Artifact) -> zipfile.ZipFile | None:
        return retried(lambda: results.archive(artifact), artifact.name, sleep, results.deadline)

    folder.mkdir(parents=True, exist_ok=True)
    missing: list[str] = []
    with ThreadPoolExecutor(PARALLEL) as pool:
        for artifact, archive in zip(artifacts, pool.map(take, artifacts), strict=True):
            if archive is None:
                missing.append(artifact.name)
            else:
                archive.extractall(folder)
    print(f"Downloaded {len(artifacts) - len(missing)} of {len(artifacts)} artifacts matching {pattern} into {folder}")
    return missing


def give_up(pattern: str) -> None:
    """End the process once the deadline and its grace have passed, whatever socket operation still blocks."""
    print(f"::error::Downloading {pattern} ran past its {DEADLINE:g} s deadline.", flush=True)
    os._exit(1)


def main(argv: list[str] | None = None) -> int:
    """Download the artifacts matching the pattern argument into the folder argument; 1 names what never arrived."""
    pattern, folder = sys.argv[1:] if argv is None else argv
    deadline = time.monotonic() + DEADLINE
    results = Results(os.environ["ACTIONS_RESULTS_URL"], os.environ["ACTIONS_RUNTIME_TOKEN"], deadline)
    watchdog = threading.Timer(DEADLINE + GRACE, give_up, (pattern,))
    watchdog.daemon = True
    watchdog.start()
    try:
        missing = download(results, pattern, Path(folder))
    finally:
        watchdog.cancel()
    if missing:
        print(f"::error::{', '.join(missing)} did not arrive within {TRIES} tries before the {DEADLINE:g} s deadline.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
