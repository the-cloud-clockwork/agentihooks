import argparse
import hashlib
import io
import itertools
import json
import os
import re
import subprocess
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import yaml


def _git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def _revision(ref):
    if subprocess.run(["git", "cat-file", "-e", ref], capture_output=True).returncode:
        _git("fetch", "--no-tags", "origin", ref)
    return _git("rev-parse", ref)


def _api(path, binary=False):
    value = subprocess.check_output(["gh", "api", path], stderr=subprocess.PIPE)
    return value if binary else json.loads(value)


def _pages(path, key):
    values = []
    page = 1
    while True:
        response = _api(f"{path}?per_page=100&page={page}")
        values.extend(response[key])
        if len(values) >= response["total_count"]:
            return values
        page += 1


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
    rows = [dict(zip(axes, values, strict=True)) for values in itertools.product(*axes.values())]
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


def _authenticated(prefix, record, jobs):
    attestations = [job for job in jobs if job["name"] == "reuse" and job["conclusion"] == "success"]
    if len(attestations) != 1:
        return False
    log = _api(f"{prefix}/jobs/{attestations[0]['id']}/logs", binary=True).decode()
    digests = re.findall(r"(?m)^\\S+ required-tree-sha256=([0-9a-f]{64})$", log)
    return digests == [_digest(record)]


def _proof(path):
    with zipfile.ZipFile(io.BytesIO(_api(path, binary=True))) as archive:
        entry = archive.getinfo("provenance.json")
        if entry.file_size > 65536:
            raise ValueError("The source metadata is too large")
        return json.loads(archive.read(entry))


def _find(args, current):
    prefix = f"repos/{args.repository}/actions"
    workflow = yaml.safe_load(Path(".github/workflows/test.yml").read_text())
    runs = _api(f"{prefix}/workflows/test.yml/runs?event=pull_request&status=success&per_page=20")["workflow_runs"]
    for run in runs:
        if run["event"] != "pull_request" or run["status"] != "completed" or run["conclusion"] != "success":
            continue
        head = _revision(run["head_sha"])
        if _git("rev-parse", f"{head}:.github") != current["inputs"]["workflow"]:
            continue
        source = f"{prefix}/runs/{run['id']}"
        artifacts = _pages(f"{source}/artifacts", "artifacts")
        name = f"required-tree-{run['run_attempt']}"
        metadata = [item for item in artifacts if item["name"] == name and not item["expired"]]
        if len(metadata) != 1:
            continue
        record = _proof(f"{prefix}/artifacts/{metadata[0]['id']}/zip")
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
        tested = _revision(record["commit"])
        if _git("rev-parse", f"{tested}^{{tree}}") != current["tree"]:
            continue
        if _git("rev-parse", f"{tested}:.github") != current["inputs"]["workflow"]:
            continue
        shards = workflow["jobs"]["unit"]["strategy"]["matrix"]["shard"]
        coverage = {f"coverage-3.12-{shard}" for shard in shards}
        kept = {item["name"] for item in artifacts if not item["expired"]}
        if not coverage <= kept:
            continue
        jobs = _pages(f"{source}/attempts/{run['run_attempt']}/jobs", "jobs")
        if _passed(workflow, jobs) and _authenticated(prefix, record, jobs):
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
