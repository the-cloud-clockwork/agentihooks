import io
import itertools
import json
import os
import sys
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

VERSION = "3.12"
POLL_SECONDS = 3
JOB_CHECK_POLLS = 10
DEADLINE_SECONDS = 1200


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


def _shard_states(api: str, repo: str, run: str, token: str) -> dict[str, str | None]:
    _, _, body = _get(f"{api}/repos/{repo}/actions/runs/{run}/jobs?per_page=100", token)
    return {
        job["name"]: job["conclusion"] if job["status"] == "completed" else None
        for job in json.loads(body)["jobs"]
        if job["name"].startswith(f"unit ({VERSION}, ")
    }


def collect(
    api: str,
    repo: str,
    run: str,
    shards: int,
    out: Path,
    token: str,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str | None:
    pending = {f"coverage-{VERSION}-{shard}": f"unit ({VERSION}, {shard})" for shard in range(1, shards + 1)}
    deadline = clock() + DEADLINE_SECONDS
    etag, artifacts, passed_before = None, [], set()
    for poll in itertools.count():
        try:
            status, new_etag, body = _get(f"{api}/repos/{repo}/actions/runs/{run}/artifacts?per_page=100", token, etag)
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
                states = _shard_states(api, repo, run, token)
                waiting = set(pending.values())
                for job, conclusion in states.items():
                    if job in waiting and conclusion not in (None, "success"):
                        return f"{job} finished {conclusion}, so its coverage never arrives"
                passed = {job for job in waiting if states.get(job) == "success"}
                if passed & passed_before:
                    return f"{', '.join(sorted(passed & passed_before))} passed without coverage"
                passed_before = passed
        except (HTTPError, URLError, OSError, ValueError, KeyError) as error:
            print(f"GitHub API unreadable, retrying: {error}", flush=True)
        if clock() >= deadline:
            return f"No coverage after {DEADLINE_SECONDS} seconds for {', '.join(sorted(pending))}"
        sleep(POLL_SECONDS)


def main() -> int:
    run, shards, out = sys.argv[1], int(sys.argv[2]), Path(sys.argv[3])
    error = collect(
        os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        os.environ["GITHUB_REPOSITORY"],
        run,
        shards,
        out,
        os.environ["GH_TOKEN"],
    )
    if error:
        print(f"::error::{error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
