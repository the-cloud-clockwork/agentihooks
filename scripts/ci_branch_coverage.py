import argparse
import io
import itertools
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
import zlib
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from scripts.ci_branch_plan import COMMIT, LEG
from scripts.ci_budget import rate_limit
from scripts.ci_test_plan import _api, _strings
from tests import coverage_check, reused_coverage

ARTIFACT = re.compile(rf"branch-coverage-3\.12-({LEG.pattern})-(\d+)", re.ASCII)
PAGE = 100
# Each download holds a shard's coverage database, about 50 MB, in memory.
WORKERS = 4
ERRORS = (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError)


class Unusable(ValueError):
    pass


def _run_entry(entry: object) -> bool:
    return (
        isinstance(entry, dict)
        and type(entry.get("run")) is int
        and type(entry.get("attempt")) is int
        and isinstance(entry.get("commit"), str)
        and bool(COMMIT.fullmatch(entry["commit"]))
        and _strings(entry.get("tests"))
    )


def contributions(plan: object, deselected: object) -> dict[tuple[int, int], tuple[str, set[str]]]:
    if not isinstance(plan, dict) or not isinstance(plan.get("runs"), list) or not _strings(plan.get("tests")):
        raise Unusable("the branch plan has no runs and tests lists")
    if not isinstance(deselected, dict) or not _strings(deselected.get("branch", [])):
        raise Unusable("the deselected tests hold no valid branch list")
    carried = set(plan["tests"]).intersection(deselected.get("branch", []))
    runs: dict[tuple[int, int], tuple[str, set[str]]] = {}
    for entry in plan["runs"]:
        if not _run_entry(entry):
            raise Unusable("the branch plan holds a malformed run")
        commit, tests = runs.setdefault((entry["run"], entry["attempt"]), (entry["commit"], set()))
        if commit != entry["commit"]:
            raise Unusable(f"pull request run {entry['run']} names two commits")
        tests.update(carried.intersection(entry["tests"]))
    return {key: found for key, found in runs.items() if found[1]}


def artifacts(repository: str, run: int, attempt: int) -> list[dict]:
    found = []
    for page in itertools.count(1):
        listed = json.loads(_api(f"repos/{repository}/actions/runs/{run}/artifacts?per_page={PAGE}&page={page}"))
        for item in listed["artifacts"]:
            named = ARTIFACT.fullmatch(item["name"])
            if named and int(named[2]) == attempt and not item["expired"]:
                found.append(item)
        if len(listed["artifacts"]) < PAGE:
            return found


def carry(repository: str, artifact: dict, tests: set[str], moved: dict[str, dict[int, int] | None], out: Path) -> int:
    data = _api(f"repos/{repository}/actions/artifacts/{artifact['id']}/zip")
    with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as staged:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                archive.extractall(source)
        except (zipfile.BadZipFile, zlib.error) as error:
            raise Unusable(f"not a readable archive ({error})") from error
        databases = sorted(Path(source).glob(".coverage*"))
        if not databases:
            raise Unusable("no coverage data")
        for database in databases:
            if problem := coverage_check.problem(database):
                raise Unusable(f"{database.name} is unusable ({problem})")
        for database in databases:
            reused_coverage.keep(database, Path(staged) / database.name, tests, moved)
        captures = reused_coverage.keep_captures(Path(source) / "js-coverage", Path(staged) / "js-coverage", tests)
        shutil.copytree(staged, out)
    return captures


def _moved(repo: str, commit: str) -> dict[str, dict[int, int] | None]:
    subprocess.run(["git", "-C", repo, "fetch", "--no-tags", "--quiet", "origin", commit], check=True)
    return reused_coverage.moved_lines(Path(repo), commit, "HEAD")


def _warn(key: tuple[int, int], reason: object) -> None:
    print(
        f"::warning title=Unusable branch coverage::Pull request run {key[0]} attempt {key[1]}: {reason};"
        " that coverage is not carried."
    )


def _start(
    pool: ThreadPoolExecutor, args: argparse.Namespace, key: tuple[int, int], commit: str, tests: set[str]
) -> tuple[dict, list[tuple[str, Future]]] | None:
    run, attempt = key
    try:
        moved = _moved(args.repo, commit)
        listed = artifacts(args.repository, run, attempt)
    except ERRORS as error:
        _warn(key, error)
        return None
    if not listed:
        _warn(key, "it kept no unexpired branch-coverage-3.12 artifact")
        return None
    started = []
    for item in listed:
        out = args.out / f"branch-{run}" / f"{ARTIFACT.fullmatch(item['name'])[1]}-{attempt}"
        started.append((item["name"], pool.submit(carry, args.repository, item, tests, moved, out)))
    return moved, started


def _finish(key: tuple[int, int], commit: str, tests: set[str], moved: dict, started: list[tuple[str, Future]]) -> None:
    captures = used = 0
    for name, future in started:
        try:
            captures += future.result()
        except ERRORS as error:
            _warn(key, f"{name}: {error}")
            continue
        used += 1
    print(
        f"Pull request run {key[0]} attempt {key[1]} on {commit}: {len(tests)} reused tests, {captures} page captures"
        f" carried from {used} of {len(started)} artifacts; {len(moved)} changed files remapped"
    )


def _runs(plan: Path, deselected: Path) -> dict[tuple[int, int], tuple[str, set[str]]]:
    if not plan.is_file() or not deselected.is_file():
        return {}
    return contributions(json.loads(plan.read_text()), json.loads(deselected.read_text()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--deselected", type=Path, required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        runs = _runs(args.plan, args.deselected)
    except (OSError, ValueError) as error:
        print(f"::warning title=Unusable branch plan::{error}; no coverage is carried from pull request runs.")
        return 0
    if not runs:
        print("No tests reused from pull request runs; no coverage to carry.")
        return 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        started = [
            (key, commit, tests, _start(pool, args, key, commit, tests))
            for key, (commit, tests) in sorted(runs.items())
        ]
        for key, commit, tests, begun in started:
            if begun is not None:
                _finish(key, commit, tests, *begun)
    rate_limit.report("The branch coverage carry")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
