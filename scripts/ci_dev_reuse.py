import argparse
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from scripts import ci_reuse

PUSH_LEGS = 20


def artifact_names(shards: int = PUSH_LEGS) -> set[str]:
    names = {f"{kind}-3.12" for kind in ("split", "collected")}
    for shard in range(1, shards + 1):
        names.update(f"{kind}-3.12-{shard}" for kind in ("durations", "flakes", "coverage"))
    return names


def _record(evidence: Path, run: dict[str, Any]) -> dict[str, Any]:
    folder = evidence / str(run["id"])
    record = ci_reuse._proof(folder / "proof.zip")
    jobs = ci_reuse._lines(folder / "jobs.jsonl")
    producer = "reuse"
    if run["event"] == "merge_group" and not any(
        job["name"] == "reuse" and job["conclusion"] == "success" for job in jobs
    ):
        producer = (
            "select" if any(job["name"] == "select" and job["conclusion"] == "success" for job in jobs) else "durations"
        )
    if not ci_reuse._authenticated(folder, record, jobs, producer=producer):
        raise ValueError("unauthenticated provenance")
    expected = {"version": 1, "run": run["id"], "attempt": run["run_attempt"], "event": run["event"]}
    if any(record.get(key) != value for key, value in expected.items()):
        raise ValueError("provenance does not describe this attempt")
    if record["inputs"]["day"] != datetime.now(UTC).date().isoformat():
        raise ValueError("dependency day changed")
    return record


def _usable(run: dict[str, Any], repository: str, event: str) -> bool:
    return (
        run["event"] == event
        and run["status"] == "completed"
        and run["conclusion"] == "success"
        and run["path"] == ".github/workflows/test.yml"
        and run["repository"]["full_name"].lower() == repository.lower()
    )


def shard_count(folder: Path) -> int:
    """The queue's unit shard count when shards 1 to that count all passed, at most twenty; else 0."""
    jobs = ci_reuse._lines(folder / "jobs.jsonl")
    units = {job["name"]: job["conclusion"] for job in jobs if job["name"].startswith("unit (")}
    shards = len(units)
    expected = {f"unit (3.12, {shard})" for shard in range(1, shards + 1)}
    if not 1 <= shards <= PUSH_LEGS or set(units) != expected or set(units.values()) != {"success"}:
        return 0
    return shards


def _complete(evidence: Path, run: dict[str, Any]) -> bool:
    folder = evidence / str(run["id"])
    artifacts = ci_reuse._lines(folder / "artifacts.jsonl")
    if any(item["name"].startswith("reused-branch-") for item in artifacts):
        return False
    kept = {item["name"] for item in artifacts if not item["expired"]}
    if any(name.startswith("reused-tests-") for name in kept):
        return False
    shards = shard_count(folder)
    return bool(shards) and artifact_names(shards) <= kept


def select_source(evidence: Path, head: str, repository: str) -> int | None:
    try:
        runs = json.loads((evidence / "runs.json").read_text())["workflow_runs"]
    except ci_reuse.UNUSABLE + (TypeError,):
        return None
    if not isinstance(runs, list):
        return None
    for run in cast(list[Any], runs):
        if not isinstance(run, dict):
            continue
        try:
            if not _usable(cast(dict[str, Any], run), repository, "merge_group") or run["head_sha"] != head:
                continue
            record = _record(evidence, cast(dict[str, Any], run))
            if record["commit"] != head:
                continue
            if record["reused"]:
                folder = evidence / str(cast(dict[str, Any], run)["id"])
                source = json.loads((folder / "source.json").read_text())
                logged = re.findall(r"(?m)^\S+ run=([0-9]+)$", (folder / "reuse.log").read_text())
                if logged != [str(source["id"])] or not _usable(source, repository, "pull_request"):
                    continue
                source_record = _record(evidence, source)
                if (
                    source_record["reused"]
                    or source_record["tree"] != record["tree"]
                    or source_record["inputs"] != record["inputs"]
                ):
                    continue
                run = source
            if _complete(evidence, cast(dict[str, Any], run)):
                print(f"Reusing unit artifacts from passed queue evidence on {head}: run {run['id']}")
                return cast(dict[str, Any], run)["id"]
        except ci_reuse.UNUSABLE + (TypeError, AttributeError) as exc:
            print(f"No dev unit reuse from run {cast(dict[str, Any], run).get('id')}: {exc}")
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--repository", required=True)
    args = parser.parse_args(argv)
    source = select_source(args.evidence, args.head, args.repository)
    if destination := os.environ.get("GITHUB_OUTPUT"):
        with Path(destination).open("a") as output:
            output.write(f"source={source or ''}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
