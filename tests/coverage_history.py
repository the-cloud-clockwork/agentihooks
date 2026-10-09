import os
import subprocess
import time
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

from tests.coverage_grade import HISTORY, Measurement, Source, executed

SEARCH = 60
WINDOW = 4
ATTEMPTS = 4


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def renamed(repo: Path, base: str) -> dict[str, str]:
    listed = git("diff", "--name-status", "-M", "--diff-filter=R", base, "HEAD", cwd=repo)
    return {old: new for _, old, new in (line.split("\t") for line in listed.splitlines())}


def _show(repo: Path, commit: str) -> Source:
    def source(path: str) -> str | None:
        shown = subprocess.run(["git", "show", f"{commit}:{path}"], cwd=repo, capture_output=True, text=True)
        return shown.stdout if shown.returncode == 0 else None

    return source


def _gh(*args: str) -> str:
    for attempt in range(ATTEMPTS):
        done = subprocess.run(["gh", *args], capture_output=True, text=True)
        if done.returncode == 0:
            return done.stdout.strip()
        print(f"::warning::gh {args[0]} failed ({done.stderr.strip()[:200]}), attempt {attempt + 1} of {ATTEMPTS}")
        time.sleep(2**attempt)
    raise subprocess.CalledProcessError(done.returncode, ["gh", *args], done.stdout, done.stderr)


def _passed_run(repo: Path, commit: str) -> str | None:
    tree = git("rev-parse", f"{commit}^{{tree}}", cwd=repo).strip()
    found = _gh(
        "api",
        f"repos/{os.environ['GITHUB_REPOSITORY']}/actions/artifacts?name=tests-passed-{tree}",
        "--jq",
        "[.artifacts[] | select(.expired | not) | select(.workflow_run.head_repository_id"
        " == .workflow_run.repository_id)] | first.workflow_run.id // empty",
    )
    return found or None


def _download(run: str, shards: int, into: Path) -> list[Path] | None:
    names = [arg for shard in range(1, shards + 1) for arg in ("--name", f"coverage-3.12-{shard}")]
    fetched = subprocess.run(
        ["gh", "run", "download", run, "--repo", os.environ["GITHUB_REPOSITORY"], *names, "--dir", str(into)],
        capture_output=True,
        text=True,
    )
    files = [into / f"coverage-3.12-{shard}" / ".coverage" for shard in range(1, shards + 1)]
    return files if fetched.returncode == 0 and all(f.is_file() for f in files) else None


def dev_runs(repo: Path, base: str, shards: int, scratch: Path) -> Iterator[Measurement]:
    commits = git("rev-list", "--first-parent", f"--max-count={SEARCH}", base, cwd=repo).split()
    # Reading coverage data holds the GIL, so threads only download and processes read.
    with ThreadPoolExecutor(max_workers=WINDOW) as pool, ProcessPoolExecutor(max_workers=WINDOW) as readers:
        fetch = _fetcher(repo, shards, scratch, lambda files: readers.submit(executed, files).result())

        def measure(commit: str) -> tuple[bool, Measurement | None]:
            run = _passed_run(repo, commit)
            return run is not None, fetch((commit, run)) if run else None

        _, first = measure(commits[0])
        if first is None:
            return
        yield first
        yield from _older(pool, measure, commits[1:])


def _older(
    pool: ThreadPoolExecutor, measure: Callable[[str], tuple[bool, Measurement | None]], commits: list[str]
) -> Iterator[Measurement]:
    # Each lookup and download spends the repository's shared Actions API quota: at most WINDOW run past the last pull.
    pending = deque(pool.submit(measure, commit) for commit in commits[:WINDOW])
    queued = iter(commits[WINDOW:])
    attempted = 0
    while pending and attempted < HISTORY:
        found, measurement = pending.popleft().result()
        if (commit := next(queued, None)) is not None:
            pending.append(pool.submit(measure, commit))
        attempted += found
        if measurement:
            yield measurement


def _fetcher(
    repo: Path, shards: int, scratch: Path, read: Callable[[list[Path]], dict[str, set[int]]]
) -> Callable[[tuple[str, str]], Measurement | None]:
    def fetch(pair: tuple[str, str]) -> Measurement | None:
        commit, run = pair
        files = _download(run, shards, scratch / run)
        if not files:
            return None
        print(f"dev {commit[:12]} measured by run {run}", flush=True)
        return Measurement(commit, read(files), _show(repo, commit))

    return fetch
