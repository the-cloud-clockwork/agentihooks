import argparse
import io
import itertools
import json
import os
import subprocess
import sys
import zipfile
import zlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scripts.ci_budget.jobs import read_jobs
from tests import dep_manifest

UNIT_JOBS = frozenset(f"unit (3.12, {shard})" for shard in range(1, 21))
# Full scope runs carry these into their own coverage for the tests they reuse.
COVERAGE = frozenset(f"coverage-3.12-{shard}" for shard in range(1, 21))
CANDIDATES = 10
REQUESTS = 100
PAGE_REQUESTS = 1 + 3 * CANDIDATES
JOB_PAGE = 100


class UnusableRecord(ValueError):
    pass


def _api(endpoint: str) -> bytes:
    try:
        return subprocess.run(["gh", "api", endpoint], capture_output=True, check=True, timeout=60).stdout
    except subprocess.CalledProcessError as error:
        print(error.stderr.decode(), file=sys.stderr)
        raise


def _strings(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _valid_test(record: object, unit_count: int) -> bool:
    if not isinstance(record, dict):
        return False
    units = record.get("units")
    return (
        _strings(record.get("files"))
        and isinstance(record.get("escapes"), bool)
        and isinstance(units, list)
        and all(type(unit) is int and 0 <= unit < unit_count for unit in units)
    )


def _valid_manifest(manifest: object) -> bool:
    if not isinstance(manifest, dict):
        return False
    tests = manifest.get("tests")
    return (
        isinstance(manifest.get("commit"), str)
        and bool(manifest["commit"])
        and _strings(manifest.get("units"))
        and isinstance(tests, dict)
        and bool(tests)
        and all(_valid_test(record, len(manifest["units"])) for record in tests.values())
    )


def _manifest(data: bytes) -> dict:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
    except (zipfile.BadZipFile, zlib.error, KeyError, ValueError) as error:
        raise UnusableRecord(str(error)) from error
    if not _valid_manifest(manifest):
        raise UnusableRecord("dependency manifest schema is invalid or empty")
    return manifest


def _recorded(run: dict, repository: str) -> bool:
    return (
        run["event"] == "push"
        and run["head_branch"] == "dev"
        and run["status"] == "completed"
        and run["path"] == ".github/workflows/test.yml"
        and run["repository"]["full_name"].lower() == repository.lower()
    )


def _usable(pool: ThreadPoolExecutor, actions: str, runs: list[dict], tally: Counter) -> tuple[dict, dict] | None:
    listings = [pool.submit(_api, f"{actions}/runs/{run['id']}/artifacts?per_page=100") for run in runs]
    tally["requests"] += len(runs)
    for run, listing in zip(runs, listings):
        tally["searched"] += 1
        artifacts = json.loads(listing.result())["artifacts"]
        kept = [item for item in artifacts if item["name"] == "dep-manifest" and not item["expired"]]
        live = {item["name"] for item in artifacts if not item["expired"]}
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


def find_record(repository: str, before: str) -> tuple[dict, dict] | None:
    actions = f"repos/{repository}/actions"
    tally, seen = Counter(), set()
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
        chosen = dep_manifest.plan(
            manifest, dep_manifest.changes(args.repo, manifest["commit"], args.head), str(run["id"])
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
