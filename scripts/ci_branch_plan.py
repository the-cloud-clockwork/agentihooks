import argparse
import io
import json
import os
import re
import subprocess
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from itertools import takewhile
from pathlib import Path
from time import monotonic, sleep
from urllib.parse import urlencode

from scripts.ci_budget import rate_limit
from scripts.ci_test_plan import UnusableRecord, _api, _strings, _valid_test
from tests import dep_manifest

PREFIX = "branch-record-3.12-"
RUNS = 8
LISTED = 30
PAGE_SIZE = 100
PAGES = 5
REQUESTS = 100
POLL_SECONDS = 15
COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
LEG = re.compile(r"\d+", re.ASCII)
LEG_JOB = re.compile(rf"(?:unit|unit-selected) \(3\.12, ({LEG.pattern})\)", re.ASCII)
TEST_STEPS = ("Run the selected tests", "Run tests")
UNUSABLE = (subprocess.SubprocessError, OSError, KeyError, TypeError, ValueError, AttributeError)


class Barrier(Exception):
    def __init__(self, reason: str, vetoes: set[str] | None = None):
        super().__init__(reason)
        self.vetoes = vetoes or set()


def _earlier(run: dict, args: argparse.Namespace) -> bool:
    return (
        run["id"] != args.run
        and run["event"] == "pull_request"
        and run["path"] == ".github/workflows/test.yml"
        and run["head_branch"] == args.head_ref
        and run["head_repository"]
        and run["head_repository"]["full_name"].lower() == args.head_repo.lower()
        and any(pull.get("number") == args.pull for pull in run["pull_requests"])
        and run["created_at"] < args.created
    )


def _runs(actions: str, args: argparse.Namespace) -> list[dict]:
    query = urlencode({"branch": args.head_ref, "event": "pull_request", "per_page": LISTED})
    listed = json.loads(_api(f"{actions}/workflows/test.yml/runs?{query}"))["workflow_runs"]
    return [run for run in listed if _earlier(run, args)][:RUNS]


def _paged(endpoint: str, key: str) -> tuple[list[dict], int]:
    found = []
    for page in range(1, PAGES + 1):
        listed = json.loads(_api(f"{endpoint}?per_page={PAGE_SIZE}&page={page}"))[key]
        found += listed
        if len(listed) < PAGE_SIZE:
            return found, page
    raise Barrier(f"it lists more than {PAGES} pages of {key}")


def _started(job: dict) -> bool:
    return any(
        step["name"] in TEST_STEPS and (step["status"] == "in_progress" or step["conclusion"] not in (None, "skipped"))
        for step in job["steps"]
    )


def _listing(actions: str, run: dict) -> tuple[list[dict], set[str], int]:
    listed, pages = _paged(f"{actions}/runs/{run['id']}/artifacts", "artifacts")
    required = set()
    for attempt in range(1, run["run_attempt"] + 1):
        jobs, more = _paged(f"{actions}/runs/{run['id']}/attempts/{attempt}/jobs", "jobs")
        pages += more
        legs = [(LEG_JOB.fullmatch(job["name"]), job) for job in jobs]
        required |= {f"{PREFIX}{leg[1]}-{job['run_attempt']}" for leg, job in legs if leg and _started(job)}
    return [item for item in listed if item["name"] in required and not item["expired"]], required, pages


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
        and all(_valid_test(test, len(units), set()) for test in tests.values())
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


def _present(root: str, commit: str) -> bool:
    return subprocess.run(["git", "-C", root, "fetch", "--no-tags", "origin", commit]).returncode == 0


def _changes(root: str, commit: str, cache: dict) -> list | None:
    if commit not in cache:
        cache[commit] = dep_manifest.changes(root, commit, "HEAD") if _present(root, commit) else None
    return cache[commit]


def _read(pool: ThreadPoolExecutor, actions: str, run: dict, items: list[dict], args: argparse.Namespace) -> list[dict]:
    downloads = [pool.submit(_api, f"{actions}/artifacts/{item['id']}/zip") for item in items]
    records = []
    for item, download in zip(items, downloads):
        try:
            records.append(_record(download.result(), item["name"], run, args))
        except UNUSABLE as error:
            raise Barrier(f"{item['name']}: {error}") from error
    return sorted(records, key=lambda record: -record["attempt"])


def _nodes(tests) -> set[str]:
    return {dep_manifest.node(test) for test in tests}


def _blocked(record: dict) -> set[str]:
    return _nodes(record["failed"]) | _nodes(record["flaky"])


def _outcomes(record: dict, changed: list) -> tuple[set[str], set[str]]:
    hit, _ = dep_manifest.invalidated(record["manifest"], changed)
    known = _nodes(record["manifest"]["tests"])
    valid = known - _nodes(hit)
    blocked = _blocked(record)
    return _nodes(record["passed"]) & valid, blocked & valid | blocked - known


def _evaluate(root: str, records: list[dict], cache: dict) -> list[tuple[dict, set[str], set[str]]]:
    usable, missing, unfetched = [], [], set()
    for record in records:
        changed = _changes(root, record["commit"], cache)
        if changed is None:
            missing.append(record["commit"])
            unfetched |= _blocked(record)
        else:
            summary = {key: record[key] for key in ("run", "attempt", "commit", "head")}
            usable.append((summary, *_outcomes(record, changed)))
    if missing:
        raise Barrier(f"commit {missing[0]} could not be fetched", unfetched.union(*(v for _, _, v in usable)))
    return usable


def _warn(run: dict, reason: object) -> None:
    print(
        f"::warning title=Unusable branch record::Pull request run {run['id']}: {reason}; neither it nor any older"
        " run is reused."
    )


def _settled(actions: str, args: argparse.Namespace) -> tuple[list[dict], int]:
    deadline = monotonic() + args.wait_seconds
    runs, polls = _runs(actions, args), 1
    while any(run["status"] != "completed" for run in runs) and monotonic() < deadline:
        sleep(POLL_SECONDS)
        runs, polls = _runs(actions, args), polls + 1
    return runs, polls


def considered(args: argparse.Namespace) -> tuple[list[tuple[dict, set[str], set[str]]], set[str]]:
    actions = f"repos/{args.repository}/actions"
    found, cache = [], {}
    with ThreadPoolExecutor() as pool:
        runs, requests = _settled(actions, args)
        done = takewhile(lambda run: run["status"] == "completed", runs)
        listings = {run["id"]: pool.submit(_listing, actions, run) for run in done}
        requests += sum(listing.result()[2] for listing in listings.values() if listing.exception() is None)
        for run in runs:
            try:
                if run["status"] != "completed":
                    raise Barrier(f"it was still {run['status']} after waiting {args.wait_seconds} seconds")
                items, required, _ = listings[run["id"]].result()
                if missing := sorted(required - {item["name"] for item in items}):
                    raise Barrier(f"started test legs left no {', '.join(missing)}")
                if requests + len(items) > REQUESTS:
                    print(f"Stopped at pull request run {run['id']}: its records would pass {REQUESTS} GitHub requests")
                    break
                requests += len(items)
                found += _evaluate(args.root, _read(pool, actions, run, items, args), cache)
            except Barrier as barrier:
                _warn(run, barrier)
                return found, barrier.vetoes
            except UNUSABLE as error:
                _warn(run, f"{type(error).__name__}: {error}")
                return found, set()
    return found, set()


def choose(found: list[tuple[dict, set[str], set[str]]], vetoed: set[str], dev: set[str]) -> list[dict]:
    vetoed = vetoed.union(*(vetoes for _, _, vetoes in found))
    entries, taken = {}, set()
    for record, passed, _ in found:
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


def _refused(args: argparse.Namespace) -> str | None:
    if args.event != "pull_request":
        return f"A {args.event or 'unnamed'} run reuses no earlier pull request run."
    if args.head_repo.lower() != args.repository.lower():
        return f"Pull request {args.pull} comes from {args.head_repo}, so it reuses no earlier run."
    return None


def branch_plan(args: argparse.Namespace) -> dict:
    if reason := _refused(args):
        print(f"::notice title=No branch record::{reason}")
        return {"version": 1, "runs": [], "tests": []}
    dev = set()
    if args.dev_plan and args.dev_plan.is_file():
        dev = _nodes(json.loads(args.dev_plan.read_text())["tests"])
    entries = choose(*considered(args), dev)
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
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME"))
    parser.add_argument("--dev-plan", type=Path)
    parser.add_argument("--wait-seconds", type=int, default=240)
    parser.add_argument("--root", required=True)
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
