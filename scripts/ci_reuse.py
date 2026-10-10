import argparse
import hashlib
import itertools
import json
import os
import re
import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import yaml

EVIDENCE = ("artifacts.jsonl", "proof.zip", "jobs.jsonl", "reuse.log")


def gate_steps() -> list[dict]:
    return [
        {
            "name": "Require every gate to succeed",
            "env": {
                "NEEDS": "${{ toJSON(needs) }}",
                "MUTATION": "${{ (github.event_name == 'pull_request' && github.base_ref == 'dev') || github.event_name == 'workflow_dispatch' }}",
                "KIND": "${{ needs.kind-due.outputs.due }}",
                "EVENT": "${{ github.event_name }}",
                "REUSED": "${{ needs.reuse.outputs.reused }}",
            },
            "run": 'echo "$NEEDS"\nif [[ "${REUSED:-false}" == true ]]; then\n  if [[ "${EVENT:-}" != merge_group ]] || ! jq -e \'has("reuse") and .reuse.result == "success" and .reuse.outputs.reused == "true" and (.reuse.outputs.run // "" | test("^[0-9]+$")) and has("queue-baseline") and ."queue-baseline".result == "success" and all(to_entries[]; ((.key == "reuse" or .key == "queue-baseline" or .key == "stage-budget") and .value.result == "success") or (.key != "reuse" and .key != "queue-baseline" and .key != "stage-budget" and .value.result == "skipped"))\' <<< "$NEEDS"; then\n    echo "::error::Verified queue reuse and its coverage baseline must succeed."\n    exit 1\n  fi\n  exit 0\nfi\nif ! jq -e --arg event "${EVENT:-}" --arg mutation "$MUTATION" --arg kind "${KIND:-}" \'all(to_entries[]; .value.result == "success" or ((.key == "mutation" or .key == "mutation-plan" or .key == "mutation-stats") and .value.result == "skipped" and $mutation == "false") or (.key == "helm-kind" and .value.result == "skipped" and $kind == "false") or (.key == "queue-baseline" and .value.result == "skipped" and $event != "merge_group"))\' <<< "$NEEDS"; then\n  echo "::error::Every required gate must succeed."\n  exit 1\nfi\n',
        }
    ]


def reuse_job() -> dict:
    return yaml.safe_load(Path(__file__).with_name("ci_reuse_job.yml").read_text())


def _git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def _revision(ref):
    if subprocess.run(["git", "cat-file", "-e", ref], capture_output=True).returncode:
        _git("fetch", "--no-tags", "origin", ref)
    return _git("rev-parse", ref)


def _lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _snapshot(args):
    base, head = _revision(args.base), _revision(args.head)
    grader = _git("rev-parse", "HEAD")
    return {
        "version": 1,
        "run": args.run,
        "attempt": args.attempt,
        "event": args.event,
        "commit": head,
        "tree": _git("rev-parse", f"{head}^{{tree}}"),
        "grader": grader,
        "inputs": {
            "base": _git("rev-parse", f"{base}^{{tree}}"),
            "grader": _git("rev-parse", f"{grader}^{{tree}}"),
            "workflow": _git("rev-parse", "HEAD:.github"),
            "day": datetime.now(UTC).date().isoformat(),
        },
    }


def _matrix(job):
    matrix = job.get("strategy", {}).get("matrix")
    if not isinstance(matrix, dict):
        return 1
    axes = {key: values for key, values in matrix.items() if key not in {"include", "exclude"}}
    if any(not isinstance(values, list) for values in axes.values()):
        return 1
    rows = [dict(pairs) for pairs in itertools.product(*([(key, value) for value in axes[key]] for key in axes))]
    rows = [
        row
        for row in rows
        if not any(all(row.get(key) == value for key, value in item.items()) for item in matrix.get("exclude", []))
    ]
    return len(rows) + len(matrix.get("include", []))


def _passed(workflow, jobs):
    definitions = workflow["jobs"]
    gate = definitions["gate-required"]
    required = gate["needs"]
    if not any(job["name"] == gate["name"] and job["conclusion"] == "success" for job in jobs):
        return False
    for name in required:
        label = definitions[name].get("name", name)
        results = [
            job["conclusion"]
            for job in jobs
            if job["name"] == label or job["name"].startswith((f"{label} (", f"{label} /"))
        ]
        if name in {"queue-baseline", "helm-kind"} and results == ["skipped"]:
            continue
        if len(results) < _matrix(definitions[name]) or any(result != "success" for result in results):
            return False
    return True


def _digest(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _authenticated(folder, record, jobs):
    attestations = [job for job in jobs if job["name"] == "reuse" and job["conclusion"] == "success"]
    if len(attestations) != 1:
        return False
    log = (folder / "reuse.log").read_text()
    digests = re.findall(r"(?m)^\S+ required-tree-sha256=([0-9a-f]{64})$", log)
    return digests == [_digest(record)]


def _proof(path):
    with zipfile.ZipFile(path) as archive:
        entry = archive.getinfo("provenance.json")
        if entry.file_size > 65536:
            raise ValueError("The source metadata is too large")
        return json.loads(archive.read(entry))


def _find(args, current):
    if _git("rev-parse", f"{current['commit']}:.github") != current["inputs"]["workflow"]:
        return None
    workflow = yaml.safe_load(Path(".github/workflows/test.yml").read_text())
    runs = json.loads((args.evidence / "runs.json").read_text())["workflow_runs"]
    for run in runs:
        if run["event"] != "pull_request" or run["status"] != "completed" or run["conclusion"] != "success":
            continue
        folder = args.evidence / str(run["id"])
        if not all((folder / name).is_file() for name in EVIDENCE):
            continue
        try:
            head = _revision(run["head_sha"])
        except subprocess.CalledProcessError:
            continue
        if _git("rev-parse", f"{head}:.github") != current["inputs"]["workflow"]:
            continue
        artifacts = _lines(folder / "artifacts.jsonl")
        name = f"required-tree-{run['run_attempt']}"
        metadata = [item for item in artifacts if item["name"] == name and not item["expired"]]
        if len(metadata) != 1:
            continue
        record = _proof(folder / "proof.zip")
        if (
            record.get("version") != 1
            or record.get("event") != "pull_request"
            or record.get("reused") is not False
            or record.get("run") != run["id"]
            or record.get("attempt") != run["run_attempt"]
            or record.get("tree") != current["tree"]
            or record.get("inputs") != current["inputs"]
        ):
            continue
        try:
            tested = _revision(record["commit"])
        except subprocess.CalledProcessError:
            continue
        if _git("rev-parse", f"{tested}^{{tree}}") != current["tree"]:
            continue
        shards = workflow["jobs"]["unit"]["strategy"]["matrix"]["shard"]
        coverage = {f"coverage-3.12-{shard}" for shard in shards}
        kept = {item["name"] for item in artifacts if not item["expired"]}
        if coverage - kept:
            continue
        jobs = _lines(folder / "jobs.jsonl")
        if _passed(workflow, jobs) and _authenticated(folder, record, jobs):
            return run
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run", type=int, required=True)
    parser.add_argument("--attempt", type=int, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--record", type=Path, required=True)
    args = parser.parse_args(argv)
    record = _snapshot(args)
    source = None
    if args.event == "merge_group":
        try:
            source = _find(args, record)
        except (subprocess.CalledProcessError, KeyError, ValueError, OSError, zipfile.BadZipFile) as exc:
            print(f"No reusable full result: {type(exc).__name__}")
    record["reused"] = source is not None
    args.record.write_text(json.dumps(record))
    print(f"required-tree-sha256={_digest(record)}")
    outputs = {
        "reused": str(record["reused"]).lower(),
        "run": str(source["id"]) if source else "",
        "attempt": str(source["run_attempt"]) if source else "",
        "grader": record["grader"],
        "recorded": "true",
    }
    for key, value in outputs.items():
        print(f"{key}={value}")
    if destination := os.environ.get("GITHUB_OUTPUT"):
        with Path(destination).open("a") as output:
            output.write("\n".join(f"{key}={value}" for key, value in outputs.items()) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
