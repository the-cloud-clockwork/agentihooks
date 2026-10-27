import argparse
import json
import math
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from scripts.ci_mutation.scope import select_tests
from scripts.ci_test_plan import find_record
from tests.shards import FIRST_SHARD_FILES, assign_files, source_sizes

PULL_REQUEST = "pull_request"
FULL = "full"
SELECTED = "selected"
NONE = "none"
DURATIONS = ".test_durations"
TESTS = "tests"
SOURCES = ("hooks", "scripts")
DOCS = "docs/"
DOC = ".md"
# A name the workflow passes to pytest through one space separated output line.
SAFE = re.compile(r"[A-Za-z0-9_./-]+")
# Changing any of these changes how every test runs, so only the full suite grades them.
HARNESS = frozenset(
    {
        "pyproject.toml",
        ".github/test-excludes.txt",
        "tests/__init__.py",
        "tests/shards.py",
        "tests/flake_ledger.py",
        "tests/pass_record.py",
        "tests/dep_inputs.py",
        "tests/browser_coverage.py",
        "tests/node_capture.cjs",
    }
)
CONFTEST = "conftest.py"
# Only the full suite ran these checks, so a new or edited test module could pass its own tier and fail the queue.
SUITE_SPEED = "tests/test_suite_speed.py"
FAKEREDIS = "fakeredis"
# Only the queue ran these whole tree checks, so a pull request adding, removing or moving a file passed its own tier.
TREE_CHECKS = (f"{TESTS}/test_codeowners.py", f"{TESTS}/test_ci_pull_request_api.py")
# Stored test seconds one leg runs inside the unit step timeout; a full shard holds about 157 and runs in 217.
LEG_SECONDS = 180.0
UNKNOWN_SECONDS = 10.0


def changed_files(root: Path, base: str, head: str) -> list[str]:
    command = ["git", "diff", "--name-only", "--no-renames", "-z", f"{base}...{head}"]
    return [name for name in subprocess.check_output(command, cwd=root).decode().split("\0") if name]


def is_test(path: str) -> bool:
    return path.endswith(".py") and path.startswith(f"{TESTS}/") and Path(path).name.startswith("test_")


def needs_full_suite(changed: list[str]) -> bool:
    return any(Path(path).name == CONFTEST or path in HARNESS for path in changed)


def is_doc(path: str) -> bool:
    return path.endswith(DOC) or path.startswith(DOCS)


def naming_tests(root: Path, needles: set[str]) -> set[str]:
    found = set()
    for test in sorted((root / TESTS).rglob("test_*.py")):
        text = test.read_text()
        if any(needle in text for needle in needles):
            found.add(test.relative_to(root).as_posix())
    return found


def reshaped(root: Path, base: str, head: str) -> bool:
    command = ["git", "diff", "--quiet", "--no-renames", "--diff-filter=AD", f"{base}...{head}"]
    result = subprocess.run(command, cwd=root)
    if result.returncode not in (0, 1):
        raise subprocess.CalledProcessError(result.returncode, command)
    return result.returncode == 1


def selected_tests(root: Path, changed: list[str], tree_reshaped: bool, manifest: dict | None = None) -> set[str]:
    tests = {path for path in changed if is_test(path)}
    if manifest is not None:
        units = {index for index, unit in enumerate(manifest["units"]) if unit.partition("::")[0] in changed}
        tests.update(
            test.partition("::")[0] for test, found in manifest["tests"].items() if units.intersection(found["units"])
        )
    if tree_reshaped:
        tests.update(TREE_CHECKS)
    needles = set()
    for path in changed:
        needles.add(path)
        if Path(path).suffix == ".py" and Path(path).parts[0] in (*SOURCES, TESTS):
            tests.update(select_tests(root, Path(path)))
        else:
            needles.add(Path(path).name)
    if any(
        path.startswith(".github/workflows/")
        or path in {".github/runner-classes.json", "scripts/ci_runner_classes.py", "scripts/ci_runner_trust.py"}
        for path in changed
    ):
        tests.update(
            {
                "tests/test_ci_anton_runners.py",
                "tests/test_ci_runner_classes.py",
                "tests/test_ci_runner_trust.py",
            }
        )
    tests |= naming_tests(root, needles)
    tests = {path for path in tests if SAFE.fullmatch(path) and (root / path).is_file()}
    if (
        tests
        and (root / SUITE_SPEED).is_file()
        and (any(is_test(path) for path in changed) or any(FAKEREDIS in (root / path).read_text() for path in tests))
    ):
        tests.add(SUITE_SPEED)
    return tests


def file_seconds(durations: dict[str, float], files: set[str]) -> dict[str, float]:
    seconds = dict.fromkeys(files, 0.0)
    for node, duration in durations.items():
        path = node.partition("::")[0]
        if path in seconds:
            seconds[path] += duration
    return {path: total if total else UNKNOWN_SECONDS for path, total in seconds.items()}


def legs_for(root: Path, tests: set[str], durations: dict[str, float], limit: int) -> list[list[str]] | None:
    seconds = file_seconds(durations, tests)
    total = sum(seconds.values())
    if total > LEG_SECONDS * limit:
        return None
    count = min(limit, max(1, math.ceil(total / LEG_SECONDS)))
    files = sorted(tests - FIRST_SHARD_FILES)
    legs = assign_files(durations, files, count, source_sizes(root, files))
    legs[0] = sorted(tests & FIRST_SHARD_FILES) + legs[0]
    return [sorted(leg) for leg in legs if leg]


def decide(
    root: Path,
    changed: list[str],
    durations: dict[str, float],
    limit: int,
    tree_reshaped: bool = False,
    manifest: dict | None = None,
) -> tuple[str, list[list[str]]]:
    if needs_full_suite(changed):
        return FULL, []
    tests = selected_tests(root, changed, tree_reshaped, manifest)
    if not tests:
        return (NONE if all(is_doc(path) for path in changed) else FULL), []
    legs = legs_for(root, tests, durations, limit)
    return (FULL, []) if legs is None else (SELECTED, legs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--legs", type=int, required=True)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--durations", type=Path, nargs="*")
    parser.add_argument("--repository", default=os.environ.get("CI_SELECT_REPOSITORY"))
    args = parser.parse_args(argv)
    scope, legs = FULL, []
    if args.event == PULL_REQUEST:
        durations = {}
        for path in args.durations or [args.root / DURATIONS]:
            durations.update(json.loads(path.read_text()))
        changed = changed_files(args.root, args.base, args.head)
        manifest = None
        if (
            args.repository
            and not needs_full_suite(changed)
            and any(path.endswith(".py") and path.startswith(SOURCES) for path in changed)
        ):
            if found := find_record(args.repository, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")):
                run, manifest = found
                print(f"Recorded coverage from dev run {run['id']} adds runtime dependent tests.")
        scope, legs = decide(
            args.root, changed, durations, args.legs, reshaped(args.root, args.base, args.head), manifest
        )
    outputs = {"scope": scope, "legs": json.dumps(list(range(1, len(legs) + 1))), "count": str(len(legs))}
    outputs.update({f"files_{index}": " ".join(leg) for index, leg in enumerate(legs, 1)})
    for key, value in outputs.items():
        print(f"{key}={value}")
    if destination := os.environ.get("GITHUB_OUTPUT"):
        with Path(destination).open("a") as output:
            output.write("".join(f"{key}={value}\n" for key, value in outputs.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
