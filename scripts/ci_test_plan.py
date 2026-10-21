import argparse
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

from scripts.ci_budget.jobs import read_jobs
from tests import dep_manifest

UNIT_JOBS = frozenset(f"unit (3.12, {shard})" for shard in range(1, 21))
# Full scope runs carry these into their own coverage for the tests they reuse.
COVERAGE = frozenset(f"coverage-3.12-{shard}" for shard in range(1, 21))
CANDIDATES = 10


def _api(endpoint: str) -> bytes:
    try:
        return subprocess.run(["gh", "api", endpoint], capture_output=True, check=True, timeout=60).stdout
    except subprocess.CalledProcessError as error:
        print(error.stderr.decode(), file=sys.stderr)
        raise


def _recorded(run: dict, repository: str) -> bool:
    return (
        run["event"] == "push"
        and run["head_branch"] == "dev"
        and run["status"] == "completed"
        and run["path"] == ".github/workflows/test.yml"
        and run["repository"]["full_name"].lower() == repository.lower()
    )


def find_record(repository: str, before: str) -> tuple[dict, dict] | None:
    actions = f"repos/{repository}/actions"
    runs = json.loads(
        _api(f"{actions}/workflows/test.yml/runs?branch=dev&event=push&status=completed&per_page={CANDIDATES}")
    )["workflow_runs"]
    for run in runs:
        if not _recorded(run, repository) or run["updated_at"] > before:
            continue
        artifacts = json.loads(_api(f"{actions}/runs/{run['id']}/artifacts?per_page=100"))["artifacts"]
        kept = [item for item in artifacts if item["name"] == "dep-manifest" and not item["expired"]]
        live = {item["name"] for item in artifacts if not item["expired"]}
        if not kept or any(name not in live for name in COVERAGE):
            continue
        jobs = read_jobs(f"{actions}/runs/{run['id']}/attempts/{run['run_attempt']}/jobs")
        if not UNIT_JOBS <= {job["name"] for job in jobs if job["conclusion"] == "success"}:
            continue
        archive = zipfile.ZipFile(io.BytesIO(_api(f"{actions}/artifacts/{kept[0]['id']}/zip")))
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("commit") != run["head_sha"]:
            print(f"Dev run {run['id']} recorded a manifest for {manifest.get('commit')}, not {run['head_sha']}")
            continue
        return run, manifest
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--before", required=True)
    args = parser.parse_args(argv)
    found = find_record(args.repository, args.before)
    reused = 0
    run_id = commit = ""
    if found is None:
        print(
            f"::notice title=No test record::No dev push run with passed unit shards kept a dependency manifest in the last {CANDIDATES}; every test runs."
        )
    else:
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
