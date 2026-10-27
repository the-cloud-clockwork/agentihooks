import argparse
import io
import json
import math
import re
import subprocess
import time
import zipfile
from pathlib import Path

from scripts.ci_mutation.stats_dependencies import CONTEXT

ARTIFACT = "mutation-test-stats-cache-"
TESTS = "actions/workflows/test.yml/runs"
SEEDED = "seeded.sha"
KEY = re.compile(r"[0-9a-f]{64}")
MERGED = re.compile(r"\(#(\d+)\)$")
DEPTH = 50
BUDGET = 300.0
TIMEOUT = 120


def api(path: str, binary: bool = False, deadline: float = math.inf):
    left = min(TIMEOUT, deadline - time.monotonic())
    if left <= 0:
        raise subprocess.TimeoutExpired(["gh", "api", path], 0)
    done = subprocess.run(
        ["gh", "api", f"repos/{{owner}}/{{repo}}/{path}"], capture_output=True, check=True, timeout=left
    )
    return done.stdout if binary else json.loads(done.stdout)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def merged_pulls(folder: Path, head: str, before: str) -> list[int]:
    seeded = folder / SEEDED
    since = seeded.read_text().strip() if seeded.is_file() else before
    if subprocess.run(["git", "merge-base", "--is-ancestor", since, head], capture_output=True).returncode:
        since = f"{head}~1"
    subjects = git("log", "--reverse", f"--max-count={DEPTH}", "--format=%s", f"{since}..{head}").splitlines()
    return [int(match.group(1)) for subject in subjects if (match := MERGED.search(subject))]


def harvest(number: int, folder: Path, taken: set[str], deadline: float = math.inf) -> int:
    try:
        sha = api(f"pulls/{number}", deadline=deadline)["head"]["sha"]
    except subprocess.CalledProcessError as error:
        if b"HTTP 404" not in error.stderr:
            raise
        print(f"Mutation stats seed: #{number} is not a readable pull request")
        return 0
    runs = api(f"{TESTS}?event=pull_request&head_sha={sha}&per_page=10", deadline=deadline)["workflow_runs"]
    for row in runs:
        artifacts = api(f"actions/runs/{row['id']}/artifacts?per_page=100", deadline=deadline)["artifacts"]
        found = [item for item in artifacts if item["name"].startswith(ARTIFACT) and not item["expired"]]
        written, pointers = 0, set()
        try:
            for item in sorted(found, key=lambda item: item["id"]):
                archive = api(f"actions/artifacts/{item['id']}/zip", binary=True, deadline=deadline)
                with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
                    for name in zipped.namelist():
                        base = Path(name).name
                        if base in taken:
                            continue
                        if Path(name).suffix in (".json", ".index") or base == CONTEXT:
                            (folder / base).write_bytes(zipped.read(name))
                            written += 1
                            if Path(name).suffix == ".index":
                                pointers.add(base)
        finally:
            taken.update(pointers)
        if found:
            return written
    return 0


def usable(folder: Path, pointer: Path, context: str | None) -> Path | None:
    key = pointer.read_text()
    record = folder / f"{key}.json"
    if not KEY.fullmatch(key) or not record.is_file():
        return None
    try:
        stored = json.loads(record.read_text())
    except ValueError:
        return None
    return record if context in (None, stored.get("context")) else None


def size(folder: Path) -> int:
    return sum(path.stat().st_size for path in folder.iterdir() if path.is_file())


def prune(folder: Path) -> int:
    marker = folder / CONTEXT
    context = marker.read_text().strip() if marker.is_file() else None
    kept = set()
    for pointer in folder.glob("*.index"):
        if record := usable(folder, pointer, context):
            kept.add(record.name)
        else:
            pointer.unlink()
    for record in folder.glob("*.json"):
        if record.name not in kept:
            record.unlink()
    return len(kept)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("--head", required=True)
    parser.add_argument("--before", required=True)
    args = parser.parse_args(argv)
    args.folder.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + BUDGET
    pulls = merged_pulls(args.folder, args.head, args.before)
    taken: set[str] = set()
    written, skipped = 0, []
    for number in reversed(pulls):
        try:
            written += harvest(number, args.folder, taken, deadline)
        except subprocess.TimeoutExpired:
            skipped.append(number)
    note = (
        f"skipped {sorted(skipped)} on a GitHub timeout or past the budget, never retried"
        if skipped
        else "none skipped"
    )
    before = size(args.folder)
    kept = prune(args.folder)
    after = size(args.folder)
    (args.folder / SEEDED).write_text(f"{args.head}\n")
    print(
        f"Mutation stats seed: {written} files from pull requests {pulls}, "
        f"{note}; kept {kept} records; "
        f"{before} bytes before the prune, {after} after"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
