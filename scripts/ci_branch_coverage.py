import argparse
import io
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
import zlib
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from scripts.ci_branch_plan import COMMIT, LEG
from scripts.ci_budget import rate_limit
from scripts.ci_test_plan import _api, _strings
from tests import coverage_check, reused_coverage
from tests.dep_manifest import node

ARTIFACT = re.compile(rf"branch-coverage-3\.12-({LEG.pattern})-(\d+)", re.ASCII)
PAGE = 100
PAGES = 5
WORKERS = 8
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
            raise Unusable(f"the branch plan holds a malformed run {json.dumps(entry)[:200]}")
        commit, tests = runs.setdefault((entry["run"], entry["attempt"]), (entry["commit"], set()))
        if commit != entry["commit"]:
            raise Unusable(f"pull request run {entry['run']} names two commits")
        tests.update(carried.intersection(entry["tests"]))
    return {key: found for key, found in runs.items() if found[1]}


def artifacts(repository: str, run: int, attempt: int) -> list[dict]:
    found = []
    for page in range(1, PAGES + 1):
        endpoint = f"repos/{repository}/actions/runs/{run}/artifacts?per_page={PAGE}&page={page}"
        listed = json.loads(_api(endpoint))["artifacts"]
        for item in listed:
            named = ARTIFACT.fullmatch(item["name"])
            if named and int(named[2]) == attempt and not item["expired"]:
                found.append(item)
        if len(listed) < PAGE:
            break
    return found


def _tests(database: Path) -> set[str]:
    with closing(sqlite3.connect(database)) as db:
        contexts = db.execute("select context from context").fetchall()
    return {node(context.rsplit("|", 1)[0]) for (context,) in contexts if context}


def carry(
    repository: str, artifact: dict, tests: set[str], moved: dict[str, dict[int, int] | None], out: Path
) -> tuple[set[str], int]:
    data = _api(f"repos/{repository}/actions/artifacts/{artifact['id']}/zip")
    with tempfile.TemporaryDirectory() as work:
        source, staged = Path(work) / "source", Path(work) / "staged"
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                archive.extractall(source)
        except (zipfile.BadZipFile, zlib.error) as error:
            raise Unusable(f"not a readable archive ({error})") from error
        databases = sorted(path for path in source.glob(".coverage*") if path.is_file())
        if not databases:
            raise Unusable("no coverage data")
        for database in databases:
            if problem := coverage_check.problem(database):
                raise Unusable(f"{database.name} is unusable ({problem})")
        kept: set[str] = set()
        for database in databases:
            reused_coverage.keep(database, staged / database.name, tests, moved)
            kept |= _tests(staged / database.name)
        captures = reused_coverage.keep_captures(source / "js-coverage", staged / "js-coverage", tests)
        shutil.copytree(staged, out, dirs_exist_ok=True)
    return kept, captures


def _moved(repo: Path, commit: str, head: str) -> dict[str, dict[int, int] | None]:
    present = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{commit}^{{commit}}"], capture_output=True)
    if present.returncode:
        fetch = ["git", "-C", str(repo), "fetch", "--no-tags", "--quiet", "origin", commit]
        subprocess.run(fetch, check=True, capture_output=True, text=True, timeout=300)
    return reused_coverage.moved_lines(repo, commit, head)


def _reason(error: Exception) -> str:
    stderr = getattr(error, "stderr", None)
    detail = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr or ""
    return f"{error} {detail.strip()}".strip()


def _warn(key: tuple[int, int], reason: str) -> None:
    run, attempt = key
    print(
        f"::warning title=Unusable branch coverage::Pull request run {run} attempt {attempt}: {reason};"
        " that coverage is not carried."
    )


def _start(
    pool: ThreadPoolExecutor, args: argparse.Namespace, key: tuple[int, int], commit: str, tests: set[str]
) -> tuple[dict, list[tuple[str, Future]]] | None:
    run, attempt = key
    try:
        moved = _moved(args.repo, commit, args.head)
        listed = artifacts(args.repository, run, attempt)
    except ERRORS as error:
        _warn(key, _reason(error))
        return None
    if not listed:
        _warn(key, "it kept no unexpired branch-coverage-3.12 artifact")
        return None
    started = []
    for item in listed:
        leg, _ = ARTIFACT.fullmatch(item["name"]).groups()
        out = args.out / f"branch-{run}" / f"{leg}-{attempt}"
        started.append((item["name"], pool.submit(carry, args.repository, item, tests, moved, out)))
    return moved, started


def _finish(key: tuple[int, int], commit: str, tests: set[str], moved: dict, started: list[tuple[str, Future]]) -> None:
    kept: set[str] = set()
    captures = used = 0
    for name, future in started:
        try:
            found, count = future.result()
        except ERRORS as error:
            _warn(key, f"{name}: {_reason(error)}")
            continue
        kept |= found
        captures += count
        used += 1
    print(
        f"Pull request run {key[0]} attempt {key[1]} on {commit}: kept the coverage of {len(kept)} of {len(tests)}"
        f" reused tests and {captures} page captures from {used} of {len(started)} artifacts;"
        f" {len(moved)} changed files remapped"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="carry the coverage earlier runs of this pull request measured")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--plan", type=Path, required=True, help="reused-branch.json from the branch planner")
    parser.add_argument("--deselected", type=Path, required=True, help="the deselect output this run skipped by")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--out", type=Path, required=True, help="the folder the shard coverage is merged from")
    args = parser.parse_args(argv)
    runs = {}
    if args.plan.is_file() and args.deselected.is_file():
        try:
            runs = contributions(json.loads(args.plan.read_text()), json.loads(args.deselected.read_text()))
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
