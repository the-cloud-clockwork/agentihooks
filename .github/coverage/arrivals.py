"""Take each unit shard's coverage as its artifact lands, so the merge starts at the last shard's end.

The sonar job starts beside the shards. This runs inside an actions/github-script step, which
hands it the job's runtime token, and asks the results service download-artifact uses for this
run's artifacts every few seconds. It holds no GitHub token for that listing, so it spends no
REST quota there. Each shard's coverage is downloaded into .coverage-shards when it is listed. A
shard ends when its coverage is taken, or when its flake report, uploaded after its coverage, is
listed with no coverage of it; a rerun counts only a flake report that changed since its first
listing. While shards still run it flattens the databases it holds as flat-<name> beside them,
which combine.sh takes as they are. A shard job still queued for a runner does not spend the
wait: this module takes no GitHub token and makes no API call of its own, so a sibling script,
queue_watch.py, polls the run's job states separately with the App token the sonar job already
mints, and leaves its answer in QUEUE_STATE on disk; only time with a fresh "started" answer
counts against the wait, and a missing or stale answer counts it as before. It returns once
every shard ended or the wait ran out, and leaves naming a shard without coverage to combine.sh.
"""

import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

SERVICE = "twirp/github.actions.results.api.v1.ArtifactService/"
INTERVAL = 3
TIMEOUT = 30
WAIT = 44 * 60
JOB_POLL = 10 * INTERVAL
QUEUE_STATE = Path(".sonar-shard-queue-state")
STALE = 3 * JOB_POLL
OUT = Path(".coverage-shards")
FLATTEN = Path(__file__).with_name("flatten.py")
FLAT = "flat-"
PART = ".part"
SELECTED = "selected"
MARKER = "flakes-3.12-{}"
PLAIN = re.compile(r"coverage-3\.12-(\d+)")
BRANCH = re.compile(r"branch-coverage-3\.12-(\d+)-\d+")
CARRIED = "branch-*/*/.coverage*"
DATABASES = (".coverage", "reused-coverage/.coverage")
OWNER = ("workflow_run_backend_id", "workflow_job_run_backend_id")


class Service(Protocol):
    """This run's artifacts: their names with ids, and a download of one into a folder."""

    def listed(self) -> dict[str, str] | None:
        """Return each artifact name with its id, or None when the service did not answer."""
        ...

    def fetch(self, name: str, folder: Path) -> bool:
        """Unpack one artifact into the folder and say whether it did."""
        ...


class Timer(Protocol):
    """The clock the wait reads and the pause it takes between listings."""

    def now(self) -> float:
        """Return the current time in seconds."""
        ...

    def pause(self, seconds: float) -> None:
        """Wait the given seconds."""
        ...


class Jobs(Protocol):
    """Whether this run's remaining shard jobs have started, read apart from the artifact listing."""

    def started(self) -> bool | None:
        """Say whether a shard job of this scope is running or done, or None when that could not be read."""
        ...


class Wall(Timer):
    """The monotonic clock and a real sleep."""

    def now(self) -> float:
        """Return the monotonic clock in seconds."""
        return time.monotonic()

    def pause(self, seconds: float) -> None:
        """Sleep the given seconds."""
        time.sleep(seconds)


def backend_ids(token: str) -> dict[str, str]:
    """Return the run and job backend ids the runtime token is scoped to."""
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    scope = next(item for item in claims["scp"].split() if item.startswith("Actions.Results:"))
    _, run, job = scope.split(":")
    return {"workflow_run_backend_id": run, "workflow_job_run_backend_id": job}


class Results:
    """The results service, reached with the job's runtime token."""

    def __init__(self, base: str, token: str) -> None:
        """Keep the service address, the token and the backend ids every request names."""
        self.base = base
        self.token = token
        self.ids = backend_ids(token)
        self.owners: dict[str, dict[str, str]] = {}

    def call(self, method: str, body: dict[str, str]) -> dict[str, Any] | None:
        """Post one request; None when the service did not answer 200 with JSON."""
        request = urllib.request.Request(
            urllib.parse.urljoin(self.base, SERVICE + method),
            data=json.dumps({**self.ids, **body}).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.loads(response.read() or b"{}")
        except (OSError, ValueError):
            return None

    def listed(self) -> dict[str, str] | None:
        """Return each artifact name of this run with its newest id, or None when the service did not answer."""
        answer = self.call("ListArtifacts", {})
        if answer is None:
            return None
        newest: dict[str, str] = {}
        for artifact in answer.get("artifacts") or []:
            name, ident = artifact["name"], str(artifact["database_id"])
            if int(ident) >= int(newest.get(name, "0")):
                newest[name] = ident
                self.owners[name] = {key: str(artifact[key]) for key in OWNER}
        return newest

    def fetch(self, name: str, folder: Path) -> bool:
        """Download one listed artifact, signed for the job that uploaded it, into the folder; False leaves no folder."""
        if name not in self.owners:
            return False
        url = (self.call("GetSignedArtifactURL", {**self.owners[name], "name": name}) or {}).get("signed_url")
        if not isinstance(url, str) or not url:
            return False
        staged = folder.with_name(folder.name + PART)
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
                archive = zipfile.ZipFile(io.BytesIO(response.read()))
            archive.extractall(staged)
        except (OSError, zipfile.BadZipFile):
            shutil.rmtree(staged, ignore_errors=True)
            return False
        shutil.rmtree(folder, ignore_errors=True)
        staged.rename(folder)
        return True


class FileJobs:
    """A shard queue state a sibling watcher writes to disk, since this module takes no GitHub token."""

    def __init__(self, path: Path) -> None:
        """Keep the path the watcher keeps current."""
        self.path = path

    def started(self) -> bool | None:
        """Say whether the watcher's last fresh answer was started, or None when it is missing or stale."""
        try:
            stat = self.path.stat()
            content = self.path.read_text().strip()
        except OSError:
            return None
        if time.time() - stat.st_mtime > STALE:
            return None
        return True if content == "started" else False if content == "queued" else None


def shard_of(name: str, scope: str, shards: int) -> int | None:
    """Return the shard a coverage artifact of this scope belongs to, or None for any other artifact."""
    patterns = (PLAIN, BRANCH) if scope == SELECTED else (PLAIN,)
    for pattern in patterns:
        if found := pattern.fullmatch(name):
            shard = int(found[1])
            return shard if 1 <= shard <= shards else None
    return None


def pending(
    listed: dict[str, str], first: dict[str, str], taken: set[str], scope: str, shards: int, attempt: int
) -> list[int]:
    """Return the shards with no coverage taken and no flake report of this attempt listed beside no coverage."""
    covered = {shard_of(name, scope, shards) for name in taken}
    offered = {shard_of(name, scope, shards) for name in listed}
    left = []
    for shard in range(1, shards + 1):
        marker = MARKER.format(shard)
        ended = marker in listed and (attempt == 1 or first.get(marker) != listed[marker]) and shard not in offered
        if shard not in covered and not ended:
            left.append(shard)
    return left


def databases(folder: Path) -> list[Path]:
    """Return the non empty coverage databases an artifact folder holds."""
    found = (folder / name for name in DATABASES)
    return [path for path in found if path.is_file() and path.stat().st_size]


class Flattening:
    """One flatten.py process writing a database's flat copy beside it."""

    def __init__(self, database: Path) -> None:
        """Start flattening the database into a partial copy."""
        self.database = database
        self.part = database.with_name(f"{FLAT}{database.name}{PART}")
        self.process = subprocess.Popen(
            [sys.executable, str(FLATTEN), str(database), str(self.part)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def done(self) -> bool:
        """Say whether the process ended, keeping its copy only when it succeeded."""
        code = self.process.poll()
        if code == 0:
            self.part.replace(self.database.with_name(f"{FLAT}{self.database.name}"))
        elif code is not None:
            self.part.unlink(missing_ok=True)
        return code is not None

    def stop(self) -> None:
        """End a process still running and drop its partial copy."""
        if not self.done():
            self.process.kill()
            self.process.wait()
            self.part.unlink(missing_ok=True)


def advance(flattening: Flattening | None, queue: list[Path]) -> Flattening | None:
    """Keep one flatten running: let a running one go on, else start the next queued database."""
    if flattening is not None and not flattening.done():
        return flattening
    return Flattening(queue.pop(0)) if queue else None


@dataclass(frozen=True)
class Scope:
    """Which shards of this run attempt are being awaited."""

    shards: int
    scope: str
    attempt: int


def arrive(
    service: Service,
    run: Scope,
    out: Path,
    seconds: float,
    timer: Timer,
    jobs: Jobs | None = None,
    raw: bool = False,
) -> list[int]:
    """Take each shard's coverage as it lands, spending the wait only while a shard job runs; return what is left."""
    out.mkdir(parents=True, exist_ok=True)
    budget_start = timer.now()
    checked = budget_start - JOB_POLL
    running = True
    taken: set[str] = set()
    first: dict[str, str] | None = None
    left = list(range(1, run.shards + 1))
    queue = [] if raw else [path for path in sorted(out.glob(CARRIED)) if path.stat().st_size]
    flattening: Flattening | None = None
    while True:
        listed = service.listed()
        if listed is not None:
            first = listed if first is None else first
            for name in sorted(listed):
                if (
                    name in taken
                    or shard_of(name, run.scope, run.shards) is None
                    or not service.fetch(name, out / name)
                ):
                    continue
                taken.add(name)
                if not raw:
                    queue.extend(databases(out / name))
                print(f"{name} landed {timer.now() - budget_start:.0f} s into the wait", flush=True)
            left = pending(listed, first, taken, run.scope, run.shards, run.attempt)
        if not left or timer.now() - budget_start >= seconds:
            break
        if not raw:
            flattening = advance(flattening, queue)
        timer.pause(INTERVAL)
        moment = timer.now()
        if jobs is not None and moment - checked >= JOB_POLL:
            checked = moment
            started = jobs.started()
            running = True if started is None else started
        if jobs is not None and not running:
            budget_start += INTERVAL
    if flattening is not None:
        flattening.stop()
    return left


def main() -> int:
    """Wait for the SHARDS shards of SCOPE in this run attempt and say how the wait ended."""
    token = os.environ.get("ACTIONS_RUNTIME_TOKEN", "")
    base = os.environ.get("ACTIONS_RESULTS_URL", "")
    if not token or not base:
        print("::error::No artifact service token reached the coverage wait; the merge finds no shard coverage.")
        return 1
    shards = int(os.environ["SHARDS"])
    scope = os.environ["SCOPE"]
    attempt = int(os.environ["GITHUB_RUN_ATTEMPT"])
    raw = os.environ.get("COVERAGE_RAW") == "true"
    service = Results(base, token)
    left = arrive(
        service, Scope(shards, scope, attempt), OUT, WAIT, Wall(), None if raw else FileJobs(QUEUE_STATE), raw
    )
    if raw:
        covered = {shard_of(path.name, scope, shards) for path in OUT.iterdir() if (path / ".coverage").is_file()}
        left = sorted(set(left) | {shard for shard in range(1, shards + 1) if shard not in covered})
    if left:
        named = ", ".join(map(str, left))
        print(f"::warning::Shards {named} had not ended after {WAIT // 60} minutes; the merge names their coverage.")
    else:
        print(f"All {shards} shards ended.", flush=True)
    return int(raw and bool(left))


if __name__ == "__main__":
    sys.exit(main())
