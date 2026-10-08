import os
import subprocess
from collections.abc import Callable, Iterator
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

from tests.coverage_grade import HISTORY, Measurement, Source, executed

SEARCH = 60


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
    return subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout.strip()


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
    with ThreadPoolExecutor(max_workers=8) as pool:
        runs = list(pool.map(lambda commit: _passed_run(repo, commit), commits))
    measured = [(commit, run) for commit, run in zip(commits, runs) if run]
    fetch = _fetcher(repo, shards, scratch, executed)
    first = next(((i, found) for i, pair in enumerate(measured) if (found := fetch(pair))), None)
    if first is None:
        return
    index, base_run = first
    yield base_run
    # Reading coverage data holds the GIL, so threads only download and processes read.
    with ThreadPoolExecutor(max_workers=8) as pool, ProcessPoolExecutor() as readers:
        fetch = _fetcher(repo, shards, scratch, lambda files: readers.submit(executed, files).result())
        yield from filter(None, pool.map(fetch, measured[index + 1 : index + 1 + HISTORY]))


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
