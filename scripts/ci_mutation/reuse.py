import argparse
import io
import json
import os
import subprocess
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

from scripts.ci_mutation.scope import PREFLIGHT, weakened_tests

EVIDENCE = Path(".mutation-gate")
REPORT = "mutation-preflight-report"
SCOPE = "scope.json"
PRODUCERS = (
    ".github/workflows/mutation-preflight.yml",
    ".github/workflows/test.yml",
    ".github/actions/",
    ".github/test-excludes.txt",
    "pyproject.toml",
    "scripts/ci_mutation/",
)
WAIT = 2400
POLL = 30


def api(path: str, binary: bool = False):
    done = subprocess.run(["gh", "api", f"repos/{{owner}}/{{repo}}/{path}"], capture_output=True, check=True)
    return done.stdout if binary else json.loads(done.stdout)


def runs(query: str, current: int) -> list[dict]:
    found = api(f"{PREFLIGHT}?event=push&status=completed&per_page=30&{query}")["workflow_runs"]
    return [row for row in found if row["id"] != current and row["conclusion"] in ("success", "failure")]


def report(run: int) -> tuple[bytes, bool] | None:
    artifacts = api(f"actions/runs/{run}/artifacts?per_page=100")["artifacts"]
    found = [artifact for artifact in artifacts if artifact["name"] == REPORT and not artifact["expired"]]
    if not found:
        return None
    archive = api(f"actions/artifacts/{max(found, key=lambda row: row['id'])['id']}/zip", binary=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        graded = json.loads(zipped.read("report.json"))
    failures = any(file["failures"] for file in graded["files"])
    return archive, not (graded["failed"] or graded["not_mutated"] or failures)


def latest(query: str, current: int) -> tuple[dict, tuple[bytes, bool]] | None:
    for row in runs(query, current):
        graded = report(row["id"])
        if graded or row["conclusion"] == "failure":
            return row, graded


def only_tests_added(source: str, head: str) -> bool:
    if subprocess.run(["git", "merge-base", "--is-ancestor", source, head]).returncode:
        return False
    command = ["git", "diff", "--name-only", "--no-renames", "-z", source, head]
    names = subprocess.check_output(command).decode().split("\0")
    return all(name.startswith("tests/") for name in names if name) and not weakened_tests(Path.cwd(), source, head)


def green(row: dict, graded: tuple[bytes, bool] | None) -> bool:
    return bool(graded and graded[1] and row["conclusion"] == "success")


def same_base(archive: bytes, row: dict, head: str) -> bool:
    if row["head_sha"] != head:
        return True
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        theirs = json.loads(zipped.read("scope.json"))["base"]
    return theirs == json.loads((EVIDENCE / "scope.json").read_text())["base"]


def in_flight(head: str, current: int) -> bool:
    found = api(f"{PREFLIGHT}?event=push&per_page=30&head_sha={head}")["workflow_runs"]
    return any(row["id"] < current and row["status"] != "completed" for row in found)


def settle(head: str, current: int) -> None:
    deadline = time.monotonic() + WAIT
    if in_flight(head, current):
        print(f"Waiting for an earlier preflight of {head}")
        time.sleep(POLL)
        while in_flight(head, current):
            if time.monotonic() >= deadline:
                print(f"Stopped waiting after {WAIT} seconds")
                return
            time.sleep(POLL)


def source(head: str, branch: str, current: int) -> tuple[dict, bytes] | None:
    settle(head, current)
    found = latest(f"head_sha={head}", current) or latest(f"branch={quote(branch)}", current)
    if found:
        row, graded = found
        if green(row, graded) and only_tests_added(row["head_sha"], head) and same_base(graded[0], row, head):
            return row, graded[0]


def scope(archive: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        return json.loads(zipped.read(SCOPE))


def exact(head: str, base: str, grader: str, branch: str, current: int) -> tuple[dict, bytes] | None:
    # The branch's own preflight produced the report, so its producer must match the protected grader's.
    if subprocess.run(["git", "diff", "--quiet", grader, head, "--", *PRODUCERS]).returncode:
        return None
    found = latest(f"head_sha={head}&branch={quote(branch)}", current)
    if found:
        row, graded = found
        if green(row, graded) and row["head_sha"] == head and scope(graded[0]) == {"base": base, "head": head}:
            return row, graded[0]


def unpack(archive: bytes, row: dict) -> None:
    EVIDENCE.mkdir(exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        for name in zipped.namelist():
            if name != SCOPE:
                zipped.extract(name, EVIDENCE)
    (EVIDENCE / "reuse.json").write_text(json.dumps({"run": row["id"], "head": row["head_sha"]}) + "\n")


def reuse(head: str, branch: str, current: int, base: str | None = None, grader: str | None = None) -> dict | None:
    try:
        found = exact(head, base, grader, branch, current) if base else source(head, branch, current)
        if found:
            unpack(found[1], found[0])
            return found[0]
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        print(f"Reuse lookup failed, mutating instead: {error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("branch")
    parser.add_argument("run", type=int)
    parser.add_argument("--head")
    parser.add_argument("--base")
    parser.add_argument("--grader")
    args = parser.parse_args()
    if any((args.head, args.base, args.grader)) and not all((args.head, args.base, args.grader)):
        parser.error("--head, --base and --grader go together")
    head = args.head or subprocess.check_output(["git", "rev-parse", "HEAD"]).decode().strip()
    row = reuse(head, args.branch, args.run, args.base, args.grader)
    if row:
        print(f"Reused the green mutation report of run {row['id']} for {row['head_sha']}")
    else:
        print("No green mutation report covers this head; mutating")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
        stream.write(f"reused={'true' if row else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
