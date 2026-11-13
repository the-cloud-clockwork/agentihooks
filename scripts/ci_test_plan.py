import argparse
import io
import itertools
import json
import os
import re
import subprocess
import sys
import time
import zipfile
import zlib
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

import scripts.ci_budget.jobs
from scripts.ci_budget import rate_limit
from tests import dep_manifest

read_jobs = cast("Callable[..., list[dict[str, Any]]]", scripts.ci_budget.jobs.read_jobs)
gh_api = cast("Callable[..., subprocess.CompletedProcess[Any]]", rate_limit.gh_api)

UNIT_JOBS = frozenset(f"unit (3.12, {shard})" for shard in range(1, 21))
# Full scope runs carry these into their own coverage for the tests they reuse.
COVERAGE = frozenset(f"coverage-3.12-{shard}" for shard in range(1, 21))
PACKED = "dep-manifest-packed"
PACKED_FILE = "packed.json"
CANDIDATES = 10
REQUESTS = 100
PAGE_REQUESTS = 1 + 3 * CANDIDATES
JOB_PAGE = 100
SERVER_ERROR = re.compile(r"HTTP 5\d\d")
SERVER_ATTEMPTS = 3
SERVER_BACKOFF = 5


class UnusableRecord(ValueError):
    pass


def _api(endpoint: str, timeout: int = 60) -> bytes:
    for attempt in range(SERVER_ATTEMPTS):
        try:
            return cast(bytes, gh_api([endpoint], timeout=timeout).stdout)
        except subprocess.CalledProcessError as error:
            stderr = error.stderr.decode()
            print(stderr, file=sys.stderr)
            if attempt + 1 == SERVER_ATTEMPTS or not SERVER_ERROR.search(stderr):
                raise
            time.sleep(SERVER_BACKOFF * 2**attempt)
    raise AssertionError("unreachable")


def _strings(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in cast(list[object], value))


def _valid_test(record: object, unit_count: int, checked: set[int]) -> bool:
    if not isinstance(record, dict):
        return False
    files, units = cast(dict[str, Any], record).get("files"), cast(dict[str, Any], record).get("units")
    # An unpacked manifest shares one files list across many tests; each list is checked once.
    if id(files) not in checked:
        if not _strings(files):
            return False
        checked.add(id(files))
    return (
        isinstance(cast(dict[str, Any], record).get("escapes"), bool)
        and isinstance(units, list)
        and set(map(type, cast(list[Any], units))) <= {int}
        and (not units or (min(cast(list[Any], units)) >= 0 and max(cast(list[Any], units)) < unit_count))
    )


def _valid_manifest(manifest: object) -> bool:
    if not isinstance(manifest, dict):
        return False
    tests, checked = cast(dict[str, Any], manifest).get("tests"), cast(set[int], set())
    return (
        isinstance(cast(dict[str, Any], manifest).get("commit"), str)
        and bool(cast(dict[str, Any], manifest)["commit"])
        and _strings(cast(dict[str, Any], manifest).get("units"))
        and isinstance(tests, dict)
        and bool(cast(dict[str, Any], tests))
        and all(
            _valid_test(record, len(cast(dict[str, Any], manifest)["units"]), checked)
            for record in cast(dict[str, Any], tests).values()
        )
    )


def _manifest(data: bytes) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if PACKED_FILE in archive.namelist():
                manifest = cast(
                    dict[str, Any],
                    cast("Callable[..., Any]", dep_manifest.unpack)(json.loads(archive.read(PACKED_FILE))),
                )
            else:
                manifest = json.loads(archive.read("manifest.json"))
    except (zipfile.BadZipFile, zlib.error, KeyError, ValueError, TypeError, IndexError, AttributeError) as error:
        raise UnusableRecord(str(error)) from error
    if not _valid_manifest(manifest):
        raise UnusableRecord("dependency manifest schema is invalid or empty")
    return manifest


def _recorded(run: dict[str, Any], repository: str) -> bool:
    return (
        run["event"] == "push"
        and run["head_branch"] == "dev"
        and run["status"] == "completed"
        and run["path"] == ".github/workflows/test.yml"
        and run["repository"]["full_name"].lower() == repository.lower()
    )


def _usable(
    pool: ThreadPoolExecutor, actions: str, runs: list[dict[str, Any]], tally: Counter[str]
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    listings = [pool.submit(_api, f"{actions}/runs/{run['id']}/artifacts?per_page=100") for run in runs]
    tally["requests"] += len(runs)
    for run, listing in zip(runs, listings):
        tally["searched"] += 1
        artifacts = cast(list[dict[str, Any]], json.loads(listing.result())["artifacts"])
        live = {item["name"]: item for item in artifacts if not item["expired"]}
        kept = [live[name] for name in (PACKED, "dep-manifest") if name in live]
        if not kept or any(name not in live for name in COVERAGE):
            tally["unavailable"] += 1
            continue
        jobs = pool.submit(read_jobs, f"{actions}/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", JOB_PAGE)
        download = pool.submit(_api, f"{actions}/artifacts/{kept[0]['id']}/zip")
        tally["requests"] += 2
        if not UNIT_JOBS <= {job["name"] for job in jobs.result() if job["conclusion"] == "success"}:
            tally["failed"] += 1
            continue
        try:
            manifest = _manifest(download.result())
        except UnusableRecord as error:
            raise UnusableRecord(f"Dev run {run['id']}: {error}") from error
        if manifest.get("commit") != run["head_sha"]:
            print(f"Dev run {run['id']} recorded a manifest for {manifest.get('commit')}, not {run['head_sha']}")
            tally["incompatible"] += 1
            continue
        return run, manifest
    return None


def find_record(repository: str, before: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    actions = f"repos/{repository}/actions"
    tally, seen = cast(Counter[str], Counter()), cast(set[int], set())
    reason = "request budget exhausted"
    with ThreadPoolExecutor(max_workers=CANDIDATES) as pool:
        for page in itertools.count(1):
            if tally["requests"] + PAGE_REQUESTS > REQUESTS:
                break
            listed = json.loads(
                _api(
                    f"{actions}/workflows/test.yml/runs?branch=dev&event=push&status=completed"
                    f"&per_page={CANDIDATES}&page={page}"
                )
            )["workflow_runs"]
            tally["requests"] += 1
            runs = [
                run
                for run in listed
                if run["id"] not in seen and _recorded(run, repository) and run["updated_at"] <= before
            ]
            seen.update(run["id"] for run in listed)
            if found := _usable(pool, actions, runs, tally):
                return found
            if len(listed) < CANDIDATES:
                reason = "no older dev push runs"
                break
    print(
        f"Searched {tally['searched']} dev push runs in {tally['requests']} of {REQUESTS} GitHub requests, {reason}:"
        f" {tally['unavailable']} kept no manifest or shard coverage, {tally['failed']} failed unit shards,"
        f" {tally['incompatible']} recorded another commit"
    )
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--before", required=True)
    args = parser.parse_args(argv)
    found = None
    try:
        found = find_record(args.repository, args.before)
    except UnusableRecord as error:
        print(f"::warning title=Unusable test record::{error}; every test runs.")
    else:
        if found is None:
            print(
                "::notice title=No test record::No dev push run with passed unit shards kept a dependency manifest; every test runs."
            )
    reused = 0
    run_id = commit = ""
    if found is not None:
        run, manifest = found
        chosen = cast(
            dict[str, Any],
            cast("Callable[..., Any]", dep_manifest.plan)(
                manifest, dep_manifest.changes(args.repo, manifest["commit"], args.head), str(run["id"])
            ),
        )
        args.out.write_text(json.dumps(chosen))
        reused = len(chosen["tests"])
        run_id, commit = str(run["id"]), manifest["commit"]
        cause = f" by {chosen['global']}" if chosen["global"] else ""
        print(
            f"Dev run {run['id']} on {manifest['commit']}: {chosen['invalidated']} of {len(manifest['tests'])}"
            f" tests invalidated{cause}, {reused} reused"
        )
    if destination := os.environ.get("GITHUB_OUTPUT"):
        with Path(destination).open("a") as output:
            output.write(f"reused={reused}\nrun={run_id}\ncommit={commit}\n")
    rate_limit.report("The test planner")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
