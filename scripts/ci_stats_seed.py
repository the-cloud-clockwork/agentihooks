import argparse
import io
import json
import re
import subprocess
import zipfile
from pathlib import Path

ARTIFACT = "mutation-test-stats-cache-"
TESTS = "actions/workflows/test.yml/runs"
SEEDED = "seeded.sha"
KEY = re.compile(r"[0-9a-f]{64}")
MERGED = re.compile(r"\(#(\d+)\)$")
DEPTH = 50


def api(path: str, binary: bool = False):
    done = subprocess.run(["gh", "api", f"repos/{{owner}}/{{repo}}/{path}"], capture_output=True, check=True)
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


def harvest(number: int, folder: Path) -> int:
    sha = api(f"pulls/{number}")["head"]["sha"]
    for row in api(f"{TESTS}?event=pull_request&head_sha={sha}&per_page=10")["workflow_runs"]:
        artifacts = api(f"actions/runs/{row['id']}/artifacts?per_page=100")["artifacts"]
        found = [item for item in artifacts if item["name"].startswith(ARTIFACT) and not item["expired"]]
        written = 0
        for item in sorted(found, key=lambda item: item["id"]):
            with zipfile.ZipFile(io.BytesIO(api(f"actions/artifacts/{item['id']}/zip", binary=True))) as zipped:
                for name in zipped.namelist():
                    if Path(name).suffix in (".json", ".index"):
                        (folder / Path(name).name).write_bytes(zipped.read(name))
                        written += 1
        if found:
            return written
    return 0


def usable(folder: Path, pointer: Path) -> Path | None:
    key = pointer.read_text()
    record = folder / f"{key}.json"
    if not KEY.fullmatch(key) or not record.is_file():
        return None
    try:
        return record if json.loads(record.read_text()).get("scope") == {} else None
    except ValueError:
        return None


def prune(folder: Path) -> int:
    kept = set()
    for pointer in folder.glob("*.index"):
        if record := usable(folder, pointer):
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
    pulls = merged_pulls(args.folder, args.head, args.before)
    written = sum(harvest(number, args.folder) for number in pulls)
    kept = prune(args.folder)
    (args.folder / SEEDED).write_text(f"{args.head}\n")
    print(f"Mutation stats seed: {written} files from pull requests {pulls}; kept {kept} records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
