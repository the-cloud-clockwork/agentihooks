import argparse
import io
import json
import os
import re
import subprocess
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

from scripts.ci_budget import rate_limit
from scripts.ci_test_plan import UnusableRecord, _api, _strings, _valid_test
from tests import dep_manifest

PREFIX = "branch-record-3.12-"
RUNS = 8
LISTED = 30
ARTIFACT_PAGE = 100
PAGES = 5
REQUESTS = 100
COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
UNUSABLE = (subprocess.SubprocessError, OSError, KeyError, TypeError, ValueError, AttributeError)


def _earlier(run: dict, args: argparse.Namespace) -> bool:
    return (
        run["id"] != args.run
        and run["event"] == "pull_request"
        and run["path"] == ".github/workflows/test.yml"
        and run["head_branch"] == args.head_ref
        and (run["head_repository"] or {}).get("full_name", "").lower() == args.head_repo.lower()
        and any(pull.get("number") == args.pull for pull in run["pull_requests"])
        and run["created_at"] < args.created
    )


def _runs(actions: str, args: argparse.Namespace) -> list[dict]:
    branch = quote(args.head_ref, safe="")
    listed = json.loads(
        _api(f"{actions}/workflows/test.yml/runs?branch={branch}&event=pull_request&per_page={LISTED}")
    )["workflow_runs"]
    return [run for run in listed if _earlier(run, args)][:RUNS]


def _artifacts(actions: str, run: dict) -> tuple[list[dict] | None, int]:
    found = []
    for page in range(1, PAGES + 1):
        listed = json.loads(_api(f"{actions}/runs/{run['id']}/artifacts?per_page={ARTIFACT_PAGE}&page={page}"))[
            "artifacts"
        ]
        found += [item for item in listed if item["name"].startswith(PREFIX) and not item["expired"]]
        if len(listed) < ARTIFACT_PAGE:
            return found, page
    return None, PAGES


def _schema(record: object) -> bool:
    if not isinstance(record, dict) or not isinstance(manifest := record.get("manifest"), dict):
        return False
    units, tests = manifest.get("units"), manifest.get("tests")
    return (
        all(type(record.get(key)) is int for key in ("version", "run", "attempt", "pull"))
        and all(isinstance(record.get(key), str) and record[key] for key in ("leg", "commit", "head", "environment"))
        and all(_strings(record.get(key)) for key in ("passed", "failed", "flaky"))
        and _strings(units)
        and isinstance(tests, dict)
        and all(_valid_test(test, len(units)) for test in tests.values())
    )


def _invalid(record: object, name: str, run: dict, args: argparse.Namespace) -> str | None:
    if not _schema(record) or record["version"] != 1:
        return "the record schema is invalid"
    expected = {"run": run["id"], "head": run["head_sha"], "pull": args.pull, "environment": args.environment}
    if differed := [key for key, value in expected.items() if record[key] != value]:
        return f"{', '.join(differed)} differed from its run"
    if not 1 <= record["attempt"] <= run["run_attempt"] or name != f"{PREFIX}{record['leg']}-{record['attempt']}":
        return "its leg and attempt do not match the artifact"
    if not COMMIT.fullmatch(record["commit"]) or record["manifest"].get("commit") != record["commit"]:
        return "its manifest does not describe its commit"
    return None


def _record(data: bytes, name: str, run: dict, args: argparse.Namespace) -> dict:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            record = json.loads(archive.read("record.json"))
    except (zipfile.BadZipFile, zlib.error, KeyError, ValueError) as error:
        raise UnusableRecord(str(error)) from error
    if reason := _invalid(record, name, run, args):
        raise UnusableRecord(reason)
    return record


def _present(root: Path, commit: str) -> bool:
    for command in (["cat-file", "-e", f"{commit}^{{commit}}"], ["fetch", "--no-tags", "origin", commit]):
        if subprocess.run(["git", "-C", str(root), *command], capture_output=True).returncode == 0:
            return True
    return False


def _changes(root: Path, record: dict, cache: dict) -> list | None:
    commit = record["commit"]
    if commit not in cache:
        cache[commit] = dep_manifest.changes(root, commit, "HEAD") if _present(root, commit) else None
        if cache[commit] is None:
            print(
                f"::warning title=Unusable branch record::Pull request run {record['run']}: commit {commit} could"
                " not be fetched; its results are not reused."
            )
    return cache[commit]


def _read(pool: ThreadPoolExecutor, actions: str, run: dict, items: list[dict], args: argparse.Namespace) -> list[dict]:
    records = []
    downloads = [pool.submit(_api, f"{actions}/artifacts/{item['id']}/zip") for item in items]
    for item, download in zip(items, downloads):
        try:
            records.append(_record(download.result(), item["name"], run, args))
        except UnusableRecord as error:
            print(
                f"::warning title=Unusable branch record::Pull request run {run['id']} {item['name']}: {error};"
                " its results are not reused."
            )
    return sorted(records, key=lambda record: -record["attempt"])


def considered(args: argparse.Namespace) -> list[tuple[dict, list]]:
    actions = f"repos/{args.repository}/actions"
    found, cache = [], {}
    with ThreadPoolExecutor(max_workers=RUNS) as pool:
        runs = _runs(actions, args)
        listings = list(pool.map(lambda run: _artifacts(actions, run), runs))
        requests = 1 + sum(pages for _, pages in listings)
        for run, (items, _) in zip(runs, listings):
            if items is None:
                print(f"::warning title=Unusable branch record::Pull request run {run['id']} lists too many artifacts")
                continue
            if requests + len(items) > REQUESTS:
                print(f"Stopped at pull request run {run['id']}: its records would pass {REQUESTS} GitHub requests")
                break
            requests += len(items)
            for record in _read(pool, actions, run, items, args):
                if (changed := _changes(args.root, record, cache)) is not None:
                    found.append((record, changed))
    return found


def _nodes(tests) -> set[str]:
    return {dep_manifest.node(test) for test in tests}


def choose(found: list[tuple[dict, list]], dev: set[str]) -> list[dict]:
    vetoed, passing = set(), []
    for record, changed in found:
        hit, _ = dep_manifest.invalidated(record["manifest"], changed)
        known = _nodes(record["manifest"]["tests"])
        valid = known - _nodes(hit)
        blocked = _nodes(record["failed"]) | _nodes(record["flaky"])
        vetoed |= blocked & valid | blocked - known
        passing.append((record, _nodes(record["passed"]) & valid))
    entries, taken = {}, set()
    for record, passed in passing:
        entry = entries.setdefault(
            (record["run"], record["attempt"]),
            {
                "run": record["run"],
                "attempt": record["attempt"],
                "commit": record["commit"],
                "head": record["head"],
                "tests": [],
            },
        )
        kept = passed - vetoed - dev - taken
        entry["tests"] = sorted(set(entry["tests"]) | kept)
        taken |= kept
    return list(entries.values())


def branch_plan(args: argparse.Namespace) -> dict:
    if args.event != "pull_request":
        print(f"::notice title=No branch record::A {args.event or 'unnamed'} run reuses no earlier pull request run.")
        return {"version": 1, "runs": [], "tests": []}
    dev = set()
    if args.dev_plan and args.dev_plan.is_file():
        dev = _nodes(json.loads(args.dev_plan.read_text())["tests"])
    entries = choose(considered(args), dev)
    for entry in entries:
        print(f"Pull request run {entry['run']} on {entry['commit']}: {len(entry['tests'])} reused")
    if not entries:
        print(
            f"::notice title=No branch record::No earlier Tests run of pull request {args.pull} left a usable"
            " branch record; no test is reused from one."
        )
    runs = [entry for entry in entries if entry["tests"]]
    return {"version": 1, "runs": runs, "tests": sorted(test for entry in runs for test in entry["tests"])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pull", type=int, required=True)
    parser.add_argument("--head-ref", required=True)
    parser.add_argument("--head-repo", required=True)
    parser.add_argument("--run", type=int, required=True)
    parser.add_argument("--created", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    parser.add_argument("--dev-plan", type=Path)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        chosen = branch_plan(args)
    except UNUSABLE as error:
        print(
            f"::warning title=Unusable branch record::{type(error).__name__}: {error}; no test is reused from an"
            " earlier pull request run."
        )
        chosen = {"version": 1, "runs": [], "tests": []}
    args.out.write_text(json.dumps(chosen))
    if destination := os.environ.get("GITHUB_OUTPUT"):
        with Path(destination).open("a") as output:
            runs = ",".join(dict.fromkeys(str(entry["run"]) for entry in chosen["runs"]))
            output.write(f"branch={len(chosen['tests'])}\nbranch_runs={runs}\n")
    rate_limit.report("The branch planner")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
