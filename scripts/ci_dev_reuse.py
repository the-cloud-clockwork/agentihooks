import argparse
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from scripts import ci_reuse


def artifact_names() -> set[str]:
    names = {f"{kind}-3.12" for kind in ("split", "collected")}
    for shard in range(1, 13):
        names.update(f"{kind}-3.12-{shard}" for kind in ("durations", "flakes", "coverage"))
    return names


def _record(evidence: Path, run: dict) -> dict:
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


def _usable(run: dict, repository: str, event: str) -> bool:
    return (
        run["event"] == event
        and run["status"] == "completed"
        and run["conclusion"] == "success"
        and run["path"] == ".github/workflows/test.yml"
        and run["repository"]["full_name"].lower() == repository.lower()
    )


def _complete(evidence: Path, run: dict) -> bool:
    folder = evidence / str(run["id"])
    kept = {item["name"] for item in ci_reuse._lines(folder / "artifacts.jsonl") if not item["expired"]}
    jobs = ci_reuse._lines(folder / "jobs.jsonl")
    expected = {f"unit (3.12, {shard})" for shard in range(1, 13)}
    passed = {job["name"] for job in jobs if job["conclusion"] == "success"}
    if any(name.startswith("reused-tests-") for name in kept):
        return False
    return artifact_names() <= kept and expected == {name for name in passed if name.startswith("unit (")}


def select_source(evidence: Path, head: str, repository: str) -> int | None:
    try:
        runs = json.loads((evidence / "runs.json").read_text())["workflow_runs"]
    except ci_reuse.UNUSABLE + (TypeError,):
        return None
    if not isinstance(runs, list):
        return None
    for run in runs:
        if not isinstance(run, dict):
            continue
        try:
            if not _usable(run, repository, "merge_group") or run["head_sha"] != head:
                continue
            record = _record(evidence, run)
            if record["commit"] != head:
                continue
            if record["reused"]:
                folder = evidence / str(run["id"])
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
            if _complete(evidence, run):
                print(f"Reusing unit artifacts from passed queue evidence on {head}: run {run['id']}")
                return run["id"]
        except ci_reuse.UNUSABLE + (TypeError, AttributeError) as exc:
            print(f"No dev unit reuse from run {run.get('id')}: {exc}")
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
