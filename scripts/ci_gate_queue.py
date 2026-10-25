import json
import os
import time
from collections.abc import Callable
from copy import deepcopy

from scripts.ci_budget.jobs import read_jobs
from scripts.ci_gate_shards import api
from scripts.ci_queue_fail_fast import logged_loss

GATE_NAME = "${{ github.event_name == 'merge_group' && 'Gate — Required' || 'queue-fail-fast' }}"
SUMMARY_NAME = "${{ github.event_name == 'merge_group' && 'Queue gate summary' || 'Gate — Required' }}"
REQUIRED = {"select", "split", "test-count", "shard-check"}
SKIPPED = {
    "unit-selected",
    "coverage-ratchet",
    "lint",
    "docs",
    "sonar",
    "mutation-plan",
    "mutation-stats",
    "mutation",
    "swarm-image",
    "worker-image",
    "brain-smoke",
    "writer-lock",
    "kind-due",
    "helm-kind",
    "semgrep",
    "ledger-load",
    "stage-budget",
    "reuse",
}


def inventory(jobs: list[dict], expected: set[str]) -> dict[str, dict]:
    groups = {}
    for job in jobs:
        name = job["name"]
        base = name.split(" (", 1)[0]
        if base not in REQUIRED | SKIPPED | {"unit", "unit rerun", "unit-lost", "queue-split"}:
            continue
        key = name if base in {"unit", "unit rerun"} and "${{" not in name else base
        if key in groups:
            raise ValueError(f"Duplicate queue job: {name}")
        groups[key] = job
        if base == "unit" and name not in expected:
            raise ValueError(f"Unplanned original shard: {name}")
        if base == "unit rerun" and key != base and name.replace("unit rerun", "unit", 1) not in expected:
            raise ValueError(f"Unplanned recovery shard: {name}")
        if base == "shard-check" and name != "shard-check (3.12)":
            raise ValueError(f"Unexpected shard checker: {name}")
    return groups


def checked(job: dict | None, conclusion: str) -> bool:
    if job is None or job["status"] != "completed":
        return False
    if job.get("conclusion") != conclusion:
        raise ValueError(f"Unexpected queue result: {job['name']}")
    return True


def originals(groups: dict[str, dict], expected: set[str], lost: Callable[[dict], bool]) -> tuple[set[str], bool]:
    recovered = set()
    finished = True
    for name in expected:
        job = groups.get(name)
        if job is None or job["status"] != "completed":
            finished = False
        elif job.get("conclusion") != "success":
            if job.get("conclusion") not in {"failure", "cancelled"} or not lost(job):
                raise ValueError(f"Original shard did not pass: {name}")
            recovered.add(name.replace("unit", "unit rerun", 1))
    return recovered, finished


def recovery(groups: dict[str, dict], recovered: set[str]) -> bool:
    reruns = {name for name in groups if name.startswith("unit rerun (")}
    if reruns - recovered:
        raise ValueError("A recovery shard did not replace a lost original")
    classified = checked(groups.get("unit-lost"), "success" if recovered else "skipped")
    if not recovered:
        skipped = checked(groups.get("unit rerun"), "skipped")
        return classified and skipped
    if "unit rerun" in groups:
        if groups["unit rerun"]["status"] != "completed":
            return False
        raise ValueError("Recovery matrix was skipped despite a lost original")
    passed = [checked(groups[name], "success") for name in reruns]
    return classified and recovered == reruns and all(passed)


def verify(jobs: list[dict], shards: list[int], lost: Callable[[dict], bool] = lambda job: False) -> bool:
    if not shards or any(type(shard) is not int or shard < 1 for shard in shards) or len(shards) != len(set(shards)):
        raise ValueError("The queue needs a nonempty unique positive shard plan")
    expected = {f"unit (3.12, {shard})" for shard in shards}
    groups = inventory(jobs, expected)
    required, skipped = REQUIRED, SKIPPED
    if "queue-split" in groups:
        required = REQUIRED - {"split"} | {"queue-split"}
        skipped = SKIPPED | {"split"}
    finished = [checked(groups.get(name), "success" if name in required else "skipped") for name in required | skipped]
    recovered, originals_finished = originals(groups, expected, lost)
    recovery_finished = recovery(groups, recovered) if originals_finished else False
    return all(finished) and originals_finished and recovery_finished


def watcher(runner: str, parallel: bool = False) -> dict:
    splitter = "queue-split" if parallel else "split"
    return {
        "name": GATE_NAME,
        "outputs": {"failed": "${{ steps.watch.outputs.failed }}"},
        "if": "${{ !cancelled() && (github.event_name != 'workflow_dispatch' || needs.admission.result == 'success') && (github.event_name != 'merge_group' || needs.reuse.outputs.reused != 'true') && github.event_name != 'pull_request' }}",
        "needs": [splitter, "select", "reuse", "admission"],
        "runs-on": runner,
        "timeout-minutes": 60,
        "steps": [
            {
                "uses": "actions/checkout@v4",
                "if": "github.event_name == 'merge_group'",
                "with": {
                    "ref": "${{ github.event.merge_group.base_sha }}",
                    "persist-credentials": False,
                },
            },
            {
                "name": "Mint the tcc main ci App token",
                "id": "app-token",
                "if": "github.event_name == 'merge_group'",
                "uses": "actions/create-github-app-token@v3.2.0",
                "with": {
                    "client-id": "${{ secrets.TCC_CI_CLIENT_ID }}",
                    "private-key": "${{ secrets.TCC_CI_APP_PRIVATE_KEY }}",
                    "repositories": "${{ github.event.repository.name }}",
                    "permission-actions": "write",
                    "permission-contents": "read",
                    "permission-pull-requests": "read",
                },
            },
            {
                "name": "Verify every queue gate while the units finish",
                "id": "watch",
                "if": "github.event_name == 'merge_group'",
                "env": {
                    "GH_TOKEN": "${{ steps.app-token.outputs.token }}",
                    "QUEUE_PLAN": "${{ format('[{0}]', needs." + splitter + ".outputs.queue_312) }}",
                },
                "run": 'python3 -m scripts.ci_gate_queue || { echo "failed=queue verification" >> "$GITHUB_OUTPUT"; exit 1; }',
            },
            {
                "name": "Cancel a rejected queue run",
                "if": "failure() && github.event_name == 'merge_group'",
                "env": {"GH_TOKEN": "${{ steps.app-token.outputs.token }}"},
                "run": 'gh run cancel "$GITHUB_RUN_ID" -R "$GITHUB_REPOSITORY"',
            },
        ],
    }


def normalize(workflows: dict[str, dict]) -> dict[str, dict]:
    normalized = deepcopy(workflows)
    for workflow in normalized.values():
        jobs = workflow.get("jobs") or {}
        dynamic = [
            job
            for job in jobs.values()
            if "Gate — Required" in job.get("name", "") and job.get("name") != "Gate — Required"
        ]
        if not dynamic:
            continue
        summary, early = jobs.get("gate-required", {}), jobs.get("queue-fail-fast", {})
        parallel = "queue-split" in jobs
        if (
            len(dynamic) != 2
            or summary.get("name") != SUMMARY_NAME
            or early != watcher(summary.get("runs-on"), parallel)
        ):
            raise ValueError("The early queue gate must use the exact reviewed context pair and verifier")
        if (
            jobs.get("reuse", {}).get("if")
            != "github.event_name != 'merge_group' && github.event_name != 'pull_request'"
        ):
            raise ValueError("The early queue gate requires the existing whole queue reuse exclusion")
        expected = REQUIRED | SKIPPED | {"unit", "unit-lost", "unit-rerun", "queue-fail-fast"}
        if parallel:
            expected = expected | {"queue-split"}
        if set(summary.get("needs", [])) != expected:
            raise ValueError("The early queue gate must preserve the reviewed graded inventory")
        summary["name"] = "Gate — Required"
        early["name"] = "queue-fail-fast"
    return normalized


def current(repository: str, ref: str, head: str) -> None:
    pages = api(f"repos/{repository}/git/matching-refs/{ref.removeprefix('refs/')}")
    heads = [row["object"]["sha"] for page in pages for row in page if row["ref"] == ref]
    if heads != [head]:
        print("::error::queue group is no longer current")
        raise ValueError("The queue group is no longer current")


def watch(repository: str, run: str, attempt: str, shards: list[int], ref: str, head: str) -> None:
    verify([], shards)
    seen = {}
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        current(repository, ref, head)
        jobs = read_jobs(f"repos/{repository}/actions/runs/{run}/attempts/{attempt}/jobs")
        if verify(jobs, shards, lost=lambda job: logged_loss(repository, job, seen)):
            current(repository, ref, head)
            return
        time.sleep(5)
    raise TimeoutError("Queue verification timed out before every expected job completed")


def main() -> None:
    if os.environ["GITHUB_EVENT_NAME"] != "merge_group":
        raise ValueError("The early queue gate runs only on merge_group")
    watch(
        os.environ["GITHUB_REPOSITORY"],
        os.environ["GITHUB_RUN_ID"],
        os.environ["GITHUB_RUN_ATTEMPT"],
        [row["shard"] for row in json.loads(os.environ["QUEUE_PLAN"])],
        os.environ["GITHUB_REF"],
        os.environ["GITHUB_SHA"],
    )


if __name__ == "__main__":
    main()
