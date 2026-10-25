from collections.abc import Callable
from copy import deepcopy

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


def verify(jobs: list[dict], shards: list[int], lost: Callable[[dict], bool] = lambda job: False) -> bool:
    if not shards or any(type(shard) is not int or shard < 1 for shard in shards) or len(shards) != len(set(shards)):
        raise ValueError("The queue needs a nonempty unique positive shard plan")
    expected = {f"unit (3.12, {shard})" for shard in shards}
    groups = {}
    for job in jobs:
        name = job["name"]
        base = name.split(" (", 1)[0]
        if base not in REQUIRED | SKIPPED | {"unit", "unit rerun", "unit-lost"}:
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
    waiting = False
    for name in REQUIRED | SKIPPED:
        job = groups.get(name)
        if job is None or job["status"] != "completed":
            waiting = True
        elif job.get("conclusion") != ("success" if name in REQUIRED else "skipped"):
            raise ValueError(f"Unexpected queue result: {job['name']}")
    recovered = set()
    for name in expected:
        job = groups.get(name)
        if job is None or job["status"] != "completed":
            waiting = True
        elif job.get("conclusion") != "success":
            if job.get("conclusion") not in {"failure", "cancelled"} or not lost(job):
                raise ValueError(f"Original shard did not pass: {name}")
            recovered.add(name.replace("unit", "unit rerun", 1))
    reruns = {name for name in groups if name.startswith("unit rerun (")}
    if reruns - recovered:
        raise ValueError("A recovery shard did not replace a lost original")
    recovery = groups.get("unit-lost")
    if recovery is None or recovery["status"] != "completed":
        waiting = True
    elif recovery.get("conclusion") != ("success" if recovered else "skipped"):
        raise ValueError("The lost runner classifier did not match the original shards")
    if recovered:
        waiting |= recovered != reruns
        for name in reruns:
            job = groups[name]
            if job["status"] != "completed":
                waiting = True
            elif job.get("conclusion") != "success":
                raise ValueError(f"Recovery shard did not pass: {name}")
        if "unit rerun" in groups:
            raise ValueError("Recovery matrix was skipped despite a lost original")
    else:
        skipped = groups.get("unit rerun")
        if skipped is None or skipped["status"] != "completed":
            waiting = True
        elif skipped.get("conclusion") != "skipped":
            raise ValueError("Recovery ran without a lost original")
    return not waiting


def watcher(runner: str) -> dict:
    return {
        "name": GATE_NAME,
        "outputs": {"failed": "${{ steps.watch.outputs.failed }}"},
        "if": "${{ !cancelled() && (github.event_name != 'workflow_dispatch' || needs.admission.result == 'success') && (github.event_name != 'merge_group' || needs.reuse.outputs.reused != 'true') && github.event_name != 'pull_request' }}",
        "needs": ["split", "select", "reuse", "admission"],
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
                    "QUEUE_PLAN": "${{ format('[{0}]', needs.split.outputs.queue_312) }}",
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
        if len(dynamic) != 2 or summary.get("name") != SUMMARY_NAME or early != watcher(summary.get("runs-on")):
            raise ValueError("The early queue gate must use the exact reviewed context pair and verifier")
        summary["name"] = "Gate — Required"
        early["name"] = "queue-fail-fast"
    return normalized
