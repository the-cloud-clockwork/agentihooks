import argparse
import io
import json
import os
import subprocess
import zipfile
from pathlib import Path
from urllib.parse import quote

from scripts.ci_mutation.scope import PREFLIGHT, weakened_tests

EVIDENCE = Path(".mutation-gate")
REPORT = "mutation-preflight-report"


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


def source(head: str, branch: str, current: int) -> tuple[dict, bytes] | None:
    found = latest(f"head_sha={head}", current) or latest(f"branch={quote(branch)}", current)
    if found:
        row, graded = found
        if graded and graded[1] and row["conclusion"] == "success" and only_tests_added(row["head_sha"], head):
            return row, graded[0]


def unpack(archive: bytes, row: dict) -> None:
    EVIDENCE.mkdir(exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        for name in zipped.namelist():
            if name != "scope.json":
                zipped.extract(name, EVIDENCE)
    (EVIDENCE / "reuse.json").write_text(json.dumps({"run": row["id"], "head": row["head_sha"]}) + "\n")


def reuse(head: str, branch: str, current: int) -> dict | None:
    try:
        found = source(head, branch, current)
        if found:
            unpack(found[1], found[0])
            return found[0]
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        print(f"Reuse lookup failed, mutating instead: {error}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("branch")
    parser.add_argument("run", type=int)
    args = parser.parse_args()
    head = subprocess.check_output(["git", "rev-parse", "HEAD"]).decode().strip()
    row = reuse(head, args.branch, args.run)
    if row:
        print(f"Reused the green mutation report of run {row['id']} for {row['head_sha']}")
    else:
        print("No green mutation report covers this head; mutating")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
        stream.write(f"reused={'true' if row else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
