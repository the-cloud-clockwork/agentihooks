import argparse
import hashlib
import itertools
import json
import os
import re
import subprocess
import zipfile
import zlib
from datetime import UTC, datetime
from pathlib import Path

import yaml

EVIDENCE = ("artifacts.jsonl", "proof.zip", "jobs.jsonl", "reuse.log")
UNUSABLE = (
    subprocess.CalledProcessError,
    KeyError,
    ValueError,
    OSError,
    zipfile.BadZipFile,
    zlib.error,
    NotImplementedError,
)

SELECTED_SCOPE = (
    '\'all(to_entries[]; .value.result == "success"',
    '\'(.select.outputs.scope // "") as $scope | ($event != "pull_request" or $scope != "selected"'
    ' or ."unit-selected".result == "success") and all(to_entries[]; .value.result == "success"',
)
SELECTED_SKIPS = (
    ')\' <<< "$NEEDS"; then\n  echo "::error::Every required gate must succeed."',
    ' or ((.key == "split" or .key == "unit" or .key == "shard-check" or .key == "coverage-ratchet")'
    ' and .value.result == "skipped" and $event == "pull_request" and ($scope == "selected" or $scope == "none"))'
    ' or (.key == "unit-selected" and .value.result == "skipped" and ($event != "pull_request" or $scope == "full" or $scope == "none"))'
    ')\' <<< "$NEEDS"; then\n  echo "::error::Every required gate must succeed."',
)
PROMOTION_REUSE = (
    re.compile(r'(?s)(if \[\[ "\$\{REUSED:-false\}" == true \]\]; then\n).*?\n  exit 0\nfi\n'),
    r'\1  echo "::error::A push or dispatch run never reuses a queue result."\n  exit 1\nfi\n',
)
PROMOTION_SKIPS = (
    '(.key == "queue-baseline" and .value.result == "skipped" and $event != "merge_group")',
    '(.key == "queue-baseline" and .value.result == "skipped" and $event != "merge_group")'
    ' or ((.key == "lint" or .key == "semgrep" or .key == "test-count") and .value.result == "skipped" and $event == "push")',
)
WATCHER = "queue-fail-fast"
WATCHER_LOSS = (
    f' elif $event == "merge_group" and $job == "{WATCHER}" then'
    ' (.value.result | IN("success", "failure", "cancelled")) and (.value.outputs.failed // "") == ""'
)

INLINE_REQUIRED = (
    '(if $mutation == "true" then ["mutation-plan", "mutation-stats", "mutation"] else [] end)',
    '(if $mutation == "true" then ["mutation-plan", "mutation-stats"]'
    ' + (if $inline == "true" then [] else ["mutation"] end) else [] end)',
)
INLINE_SKIP = (
    '.value.result == "skipped" and $mutation == "false")',
    '.value.result == "skipped" and $mutation == "false")'
    ' or (.key == "mutation" and .value.result == "skipped" and $inline == "true")',
)


QUEUE_TIER = (
    """if [[ "${EVENT:-}" == merge_group || "${EVENT:-}" == pull_request ]]; then
  if ! jq -e --arg event "$EVENT" --arg mutation "$MUTATION" '
    (.select.outputs.scope // "") as $scope
    | (if $event == "merge_group" then
      ["durations", "split", "unit", "shard-check", "test-count", \""""
    + WATCHER
    + """\"]
    else
      ["durations", "select", "test-count", "lint", "semgrep", "sonar"]
      + (if $scope == "full" then ["split", "unit", "shard-check"] elif $scope == "selected" then ["unit-selected"] else [] end)
      + (if $mutation == "true" then ["mutation-plan", "mutation-stats", "mutation"] else [] end)
    end) as $required
    | . as $jobs
    | ($event == "merge_group" or (($mutation == "true" or $mutation == "false") and ($scope == "full" or $scope == "selected" or $scope == "none")))
      and all($required[]; . as $job | $jobs[$job].result == "success")
      and all(to_entries[]; .key as $job |
        if $required | index($job) then .value.result == "success" else .value.result == "skipped" end)
  ' <<< "$NEEDS"; then
    echo "::error::Every gate required for this event must succeed and other gates must be skipped."
    exit 1
  fi
  exit 0
fi
"""
)


def gate_steps(
    selected: bool = False,
    queue_tier: bool = False,
    promotion: bool = False,
    consolidated: bool = False,
    summary: bool = False,
    docs: bool = False,
    watcher_loss: bool = False,
) -> list[dict]:
    run = 'echo "$NEEDS"\nif [[ "${REUSED:-false}" == true ]]; then\n  if [[ "${EVENT:-}" != merge_group ]] || ! jq -e \'has("reuse") and .reuse.result == "success" and .reuse.outputs.reused == "true" and (.reuse.outputs.run // "" | test("^[0-9]+$")) and has("queue-baseline") and ."queue-baseline".result == "success" and all(to_entries[]; ((.key == "reuse" or .key == "queue-baseline" or .key == "stage-budget") and .value.result == "success") or (.key != "reuse" and .key != "queue-baseline" and .key != "stage-budget" and .value.result == "skipped"))\' <<< "$NEEDS"; then\n    echo "::error::Verified queue reuse and its coverage baseline must succeed."\n    exit 1\n  fi\n  exit 0\nfi\nif ! jq -e --arg event "${EVENT:-}" --arg mutation "$MUTATION" --arg kind "${KIND:-}" \'all(to_entries[]; .value.result == "success" or ((.key == "mutation" or .key == "mutation-plan" or .key == "mutation-stats") and .value.result == "skipped" and $mutation == "false") or (.key == "helm-kind" and .value.result == "skipped" and $kind == "false") or (.key == "queue-baseline" and .value.result == "skipped" and $event != "merge_group"))\' <<< "$NEEDS"; then\n  echo "::error::Every required gate must succeed."\n  exit 1\nfi\n'
    if selected or queue_tier:
        run = run.replace(*SELECTED_SCOPE).replace(*SELECTED_SKIPS)
    if promotion:
        run = PROMOTION_REUSE[0].sub(PROMOTION_REUSE[1], run).replace(*PROMOTION_SKIPS)
    if queue_tier:
        run = QUEUE_TIER + run
    if consolidated:
        run = run.replace('["durations", "split",', '["select", "split",').replace(
            '["durations", "select",', '["select",'
        )
    if watcher_loss:
        run = (
            run.replace(f', "{WATCHER}"]', "]")
            .replace(
                "and all($required[];",
                f'and ($event != "merge_group" or has("{WATCHER}"))\n      and all($required[];',
            )
            .replace(
                'then .value.result == "success" else .value.result == "skipped" end)',
                f'then .value.result == "success"{WATCHER_LOSS} else .value.result == "skipped" end)',
            )
        )
    if docs:
        run = run.replace('"lint", "semgrep"', '"lint", "docs", "semgrep"')
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
            "run": run,
        }
    ] + (summary_steps() if summary else [])


def inline_mutation(steps: list[dict]) -> list[dict]:
    [aggregation, *rest] = steps
    run = (
        aggregation["run"]
        .replace('--arg mutation "$MUTATION"', '--arg mutation "$MUTATION" --arg inline "${INLINE:-}"')
        .replace(*INLINE_REQUIRED)
        .replace(*INLINE_SKIP)
    )
    env = {
        **aggregation["env"],
        "INLINE": "${{ needs.mutation-plan.outputs.shards == '[0]' && needs.mutation-plan.outputs.stats_parts == '[0]' }}",
    }
    return [{**aggregation, "env": env, "run": run}, *rest]


def summary_steps() -> list[dict]:
    return yaml.safe_load(Path(__file__).with_name("ci_gate_summary_steps.yml").read_text())


def reuse_job() -> dict:
    return yaml.safe_load(Path(__file__).with_name("ci_reuse_job.yml").read_text())


def duration_provenance_steps() -> list[dict]:
    return yaml.safe_load(Path(__file__).with_name("ci_duration_provenance.yml").read_text())


def paged_reuse_job() -> dict:
    job = reuse_job()
    [fetch] = [step for step in job["steps"] if step.get("name") == "Fetch the evidence of recent passed runs"]
    fetch["run"] = fetch["run"].replace(
        "gh api --paginate \"$actions/runs/$1/attempts/$2/jobs?per_page=100\" --jq '.jobs[]'",
        'python3 -m scripts.ci_budget.jobs "$actions/runs/$1/attempts/$2/jobs" --page-size 5',
    )
    return job


def _git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def _revision(ref):
    if subprocess.run(["git", "cat-file", "-e", ref], capture_output=True).returncode:
        _git("fetch", "--no-tags", "origin", ref)
    return _git("rev-parse", ref)


def _resolved(ref, suffix):
    try:
        return _git("rev-parse", f"{_revision(ref)}{suffix}")
    except subprocess.CalledProcessError:
        return None


def _lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _snapshot(args):
    base, head = _revision(args.base), _revision(args.head)
    grader = _git("rev-parse", "HEAD")
    if args.event == "pull_request":
        base = _git("merge-base", grader, head)
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
        if name in {"helm-kind", "unit-selected"} and results == ["skipped"]:
            continue
        if len(results) < _matrix(definitions[name]) or any(result != "success" for result in results):
            return False
    return True


def _digest(record):
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _authenticated(folder, record, jobs, producer="reuse"):
    attestations = [job for job in jobs if job["name"] == producer and job["conclusion"] == "success"]
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
        record = json.loads(archive.read(entry))
    if not isinstance(record, dict):
        raise ValueError("The source metadata is not an object")
    return record


def _differs(expected, actual, prefix=""):
    keys = []
    for key in sorted(set(expected) | set(actual)):
        if isinstance(expected.get(key), dict) and isinstance(actual.get(key), dict):
            keys += _differs(expected[key], actual[key], f"{prefix}{key}.")
        elif (type(expected.get(key)), expected.get(key)) != (type(actual.get(key)), actual.get(key)):
            keys.append(f"{prefix}{key}")
    return keys


def _refusal(args, current, workflow, run):
    for key, value in (("event", "pull_request"), ("status", "completed"), ("conclusion", "success")):
        if run[key] != value:
            return f"{key} is {run[key]}"
    folder = args.evidence / str(run["id"])
    if missing := [name for name in EVIDENCE if not (folder / name).is_file()]:
        return f"no {', '.join(missing)} evidence"
    workflow_files = _resolved(run["head_sha"], ":.github")
    if workflow_files is None:
        return "head commit unavailable"
    if workflow_files != current["inputs"]["workflow"]:
        return "workflow files differ at its head"
    artifacts = _lines(folder / "artifacts.jsonl")
    name = f"required-tree-{run['run_attempt']}"
    if len([item for item in artifacts if item["name"] == name and not item["expired"]]) != 1:
        return f"not exactly one {name} artifact"
    record = _proof(folder / "proof.zip")
    expected = {
        "version": 1,
        "event": "pull_request",
        "reused": False,
        "run": run["id"],
        "attempt": run["run_attempt"],
        "tree": current["tree"],
        "inputs": current["inputs"],
    }
    if keys := _differs(expected, {key: record.get(key) for key in expected}):
        return f"{', '.join(keys)} differed"
    if _resolved(record["commit"], "^{tree}") != current["tree"]:
        return "commit.tree differed"
    shards = workflow["jobs"]["unit"]["strategy"]["matrix"]["shard"]
    kept = {item["name"] for item in artifacts if not item["expired"]}
    if missing := sorted({f"coverage-3.12-{shard}" for shard in shards} - kept):
        return f"no {', '.join(missing)} artifact"
    jobs = _lines(folder / "jobs.jsonl")
    if not _passed(workflow, jobs):
        return "a required job did not pass"
    if not _authenticated(folder, record, jobs):
        return "its reuse log does not attest the record"
    return None


def _find(args, current):
    if _git("rev-parse", f"{current['commit']}:.github") != current["inputs"]["workflow"]:
        print("No reuse: the queue tree changes workflow files")
        return None
    workflow = yaml.safe_load(Path(".github/workflows/test.yml").read_text())
    runs = json.loads((args.evidence / "runs.json").read_text())["workflow_runs"]
    if not runs:
        print("No passed pull request run for this head")
    for run in runs:
        try:
            reason = _refusal(args, current, workflow, run)
        except UNUSABLE as exc:
            reason = type(exc).__name__
        if reason:
            print(f"Refused run {run['id']}: {reason}")
        else:
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
        except UNUSABLE as exc:
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
