import io
import itertools
import json
import os
import sys
import time
import zipfile
from collections.abc import Callable
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

VERSION = "3.12"
POLL_SECONDS = 3
JOB_CHECK_POLLS = 10
DEADLINE_SECONDS = 1200
ATTEMPTS = 5


def _get(url: str, token: str, etag: str | None = None) -> tuple[int, str | None, bytes]:
    request = Request(url, headers={"Accept": "application/vnd.github+json"})
    # The artifact download redirects to blob storage, which must not receive the token.
    request.add_unredirected_header("Authorization", f"Bearer {token}")
    if etag:
        request.add_unredirected_header("If-None-Match", etag)
    try:
        with urlopen(request, timeout=30) as response:
            return response.status, response.headers.get("ETag"), response.read()
    except HTTPError as error:
        if error.code == 304:
            return 304, etag, b""
        raise


def _describe(error: Exception) -> str:
    if not isinstance(error, HTTPError):
        return str(error)
    try:
        body = error.read().decode(errors="replace")[:1000]
    except (OSError, HTTPException):
        body = ""
    return f"HTTP {error.code} {error.reason}: {body}"


def _shard_states(runs_url: str, token: str) -> dict[str, str | None]:
    _, _, body = _get(f"{runs_url}/jobs?per_page=100", token)
    return {
        job["name"]: job["conclusion"] if job["status"] == "completed" else None
        for job in json.loads(body)["jobs"]
        if job["name"].startswith(f"unit ({VERSION}, ")
    }


def _check_shards(runs_url: str, token: str, waiting: set[str], passed_before: set[str]) -> tuple[str | None, set[str]]:
    states = _shard_states(runs_url, token)
    for job in sorted(waiting):
        if states.get(job) not in (None, "success"):
            return f"{job} finished {states[job]}, so its coverage never arrives", set()
    passed = {job for job in waiting if states.get(job) == "success"}
    # The artifact listing can trail a job's completion, so a passed shard gets one more check before it is red.
    if passed & passed_before:
        return f"{', '.join(sorted(passed & passed_before))} passed without coverage", set()
    return None, passed


def collect(
    runs_url: str,
    shards: int,
    out: Path,
    token: str,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str | None:
    pending = {f"coverage-{VERSION}-{shard}": f"unit ({VERSION}, {shard})" for shard in range(1, shards + 1)}
    deadline = clock() + DEADLINE_SECONDS
    etag, artifacts, passed_before, failures = None, [], set(), 0
    for poll in itertools.count():
        try:
            status, new_etag, body = _get(f"{runs_url}/artifacts?per_page=100", token, etag)
            if status == 200:
                etag, artifacts = new_etag, json.loads(body)["artifacts"]
            for artifact in artifacts:
                if artifact["name"] in pending and not artifact["expired"]:
                    _, _, archive = _get(artifact["archive_download_url"], token)
                    zipfile.ZipFile(io.BytesIO(archive)).extractall(out / artifact["name"])
                    print(f"Downloaded {artifact['name']}", flush=True)
                    del pending[artifact["name"]]
            if not pending:
                return None
            if poll % JOB_CHECK_POLLS == JOB_CHECK_POLLS - 1:
                error, passed_before = _check_shards(runs_url, token, set(pending.values()), passed_before)
                if error:
                    return error
            failures = 0
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, HTTPException) as error:
            failures += 1
            if failures >= ATTEMPTS:
                return f"GitHub API failed {failures} times in a row: {_describe(error)}"
            print(f"::warning::GitHub API unreadable, retrying: {_describe(error)}", flush=True)
        if clock() >= deadline:
            return f"No coverage after {DEADLINE_SECONDS} seconds for {', '.join(sorted(pending))}"
        sleep(POLL_SECONDS * (failures + 1))


def main() -> int:
    run, shards, out = sys.argv[1], int(sys.argv[2]), Path(sys.argv[3])
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    error = collect(
        f"{api}/repos/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{run}", shards, out, os.environ["GH_TOKEN"]
    )
    if error:
        print(f"::error::{error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
