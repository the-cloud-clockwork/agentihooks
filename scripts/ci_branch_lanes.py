import argparse
import json
import re
import subprocess
import time
from pathlib import Path

import yaml

WORKFLOWS = ".github/workflows"
LANE = (
    "format('hosted-lane-{0}', "
    "((endsWith(github.run_id, '0') || endsWith(github.run_id, '1') || endsWith(github.run_id, '2')) && 'a') || "
    "((endsWith(github.run_id, '3') || endsWith(github.run_id, '4') || endsWith(github.run_id, '5')) && 'b') || 'c')"
)
GROUP = "${{ " + LANE + " }}"
PATH_FILTERS = ("paths", "paths-ignore")


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def api(*args: str) -> dict:
    return json.loads(subprocess.run(["gh", "api", *args], capture_output=True, check=True).stdout or "{}")


def stop(repo: str, run: int) -> bool:
    argv = ["gh", "api", "-X", "POST", f"repos/{repo}/actions/runs/{run}/cancel"]
    return subprocess.run(argv, capture_output=True).returncode == 0


def branch_only(head: str, root: Path, include_edits: bool = False) -> list[str]:
    listed = git("ls-tree", "-z", "--name-only", f"{head}:{WORKFLOWS}").split("\0")
    return [
        f"{WORKFLOWS}/{name}"
        for name in listed
        if name.endswith((".yml", ".yaml"))
        and (
            not (root / WORKFLOWS / name).exists()
            or include_edits
            and (root / WORKFLOWS / name).read_text() != git("show", f"{head}:{WORKFLOWS}/{name}")
        )
    ]


def lane_problem(block: object, name: object = None) -> str | None:
    if isinstance(block, dict) and called_lane(block, name):
        block = {**block, "group": GROUP, "queue": "max"}
    if not isinstance(block, dict) or " ".join(str(block.get("group")).split()) != GROUP:
        return "has no shared hosted lane concurrency group"
    if block.get("queue") != "max":
        return "queues on the lane without queue: max"
    if block.get("cancel-in-progress") not in (None, False):
        return "may cancel other runs on the shared lane"
    return None


def called_lane(block: dict, name: object) -> bool:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9 ]+", name):
        return False
    own = re.fullmatch(
        r"\$\{\{ github\.workflow == '"
        + re.escape(name)
        + r"' && "
        + re.escape(LANE)
        + r" \|\| format\('[a-z0-9-]+-\{0\}', github\.run_id\) \}\}",
        " ".join(str(block.get("group")).split()),
    )
    return own is not None and block.get("queue") == f"${{{{ github.workflow == '{name}' && 'max' || 'single' }}}}"


def events(workflow: dict) -> list:
    return [workflow[key] for key in ("on", True) if key in workflow]


def unfiltered(on: object) -> object:
    if not isinstance(on, dict) or not isinstance(on.get("pull_request"), dict):
        return on
    kept = {key: value for key, value in on["pull_request"].items() if key not in PATH_FILTERS}
    return {**on, "pull_request": kept or None}


def deletes_path_filters(base: object, head: object) -> bool:
    old, new = (on.get("pull_request") if isinstance(on, dict) else None for on in (base, head))
    return not isinstance(new, dict) or all(
        key not in new or isinstance(old, dict) and old.get(key) == new[key] for key in PATH_FILTERS
    )


def same_events(baseline: dict, workflow: dict) -> bool:
    if events(baseline) == events(workflow):
        return True
    # Deleting a pull request path filter from an otherwise identical workflow only runs it on more pull requests.
    return without_path_filters(baseline) == without_path_filters(workflow) and all(
        deletes_path_filters(old, new) for old, new in zip(events(baseline), events(workflow))
    )


def without_path_filters(workflow: dict) -> dict:
    return {key: unfiltered(value) if key in ("on", True) else value for key, value in workflow.items()}


def per_run_mutation_group(name: str, block: object, job: dict, trusted: dict) -> bool:
    stages = {"stats": ("stats", "part"), "mutation-shard": ("shard", "shard")}
    baseline = (trusted.get("jobs") or {}).get(name)
    if trusted.get("name") != "Mutation preflight" or name not in stages or not isinstance(baseline, dict):
        return False
    if job.get("runs-on") != baseline.get("runs-on"):
        return False
    stage, index = stages[name]
    group = f"${{{{ format('mutation-preflight-job-{{0}}-{stage}-{{1}}', github.run_id, matrix.{index}) }}}}"
    superseded = {
        "group": f"${{{{ format('{{0}}-{stage}-{{1}}', github.workflow == 'Mutation preflight' && "
        f"{LANE.replace('hosted-lane-', 'mutation-lane-')} || format('mutation-preflight-job-{{0}}', github.run_id), "
        f"matrix.{index}) }}}}",
        "queue": "${{ github.workflow == 'Mutation preflight' && 'max' || 'single' }}",
    }
    return block in ({"group": group}, superseded)


def problems(workflow: object, baseline: object = None) -> list[str]:
    if not isinstance(workflow, dict):
        return ["is not a workflow mapping"]
    jobs = workflow.get("jobs") or {}
    if not isinstance(jobs, dict):
        return ["has no jobs mapping"]
    trusted = (
        baseline
        if isinstance(baseline, dict)
        and baseline.get("name") == workflow.get("name")
        and same_events(baseline, workflow)
        else {}
    )
    found = []
    for name, job in jobs.items():
        job = job if isinstance(job, dict) else {}
        block = job.get("concurrency", workflow.get("concurrency"))
        base_job = (trusted.get("jobs") or {}).get(name, {})
        base_block = base_job.get("concurrency", trusted.get("concurrency"))
        if per_run_mutation_group(name, block, job, trusted):
            continue
        if trusted.get("name") == "Tests" and name in (trusted.get("jobs") or {}) and block == {
            "group": "${{ github.event_name == 'workflow_dispatch' && "
            + LANE
            + " || format('tests-{0}-{1}', github.event_name, github.event.pull_request.number || github.run_id) }}",
            "queue": "${{ github.event_name == 'workflow_dispatch' && 'max' || 'single' }}",
            "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
        }:
            continue
        if trusted and (name in (trusted.get("jobs") or {}) or base_block is not None) and block == base_block:
            continue
        problem = lane_problem(block, workflow.get("name"))
        if problem:
            found.append(f"job {name} {problem}")
    return found


def grade(text: str, baseline: str | None = None) -> list[str]:
    try:
        return problems(yaml.safe_load(text), yaml.safe_load(baseline) if baseline is not None else None)
    except yaml.YAMLError:
        return ["does not parse as YAML"]


def reusable_problems(head: str, root: Path, workflow: object, seen: tuple[str, ...] = ()) -> list[str]:
    if not isinstance(workflow, dict) or not isinstance(workflow.get("jobs", {}), dict):
        return []
    found = []
    for name, job in (workflow.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        call = job.get("uses")
        if not isinstance(call, str) or not call.startswith(f"./{WORKFLOWS}/"):
            continue
        if lane_problem(job.get("concurrency", workflow.get("concurrency")), workflow.get("name")) is None:
            continue
        path = call[2:]
        if path in seen:
            continue
        try:
            called = yaml.safe_load(git("show", f"{head}:{path}"))
        except yaml.YAMLError:
            found.append(f"job {name} calls {path}: does not parse as YAML")
            continue
        if isinstance(called, dict):
            called = {**called, "name": workflow.get("name")}
        issues = problems(called) if not (root / path).exists() else []
        issues.extend(reusable_problems(head, root, called, (*seen, path)))
        found.extend(f"job {name} calls {path}: {issue}" for issue in issues)
    return found


def refused(head: str, root: Path) -> dict[str, list[str]]:
    graded = {
        path: grade(git("show", f"{head}:{path}"), (root / path).read_text() if (root / path).exists() else None)
        for path in branch_only(head, root, include_edits=True)
    }
    for path, found in graded.items():
        if "does not parse as YAML" not in found:
            found.extend(reusable_problems(head, root, yaml.safe_load(git("show", f"{head}:{path}")), (path,)))
    return {path: found for path, found in graded.items() if found}


def fires_on_head(head: str, path: str) -> bool:
    try:
        workflow = yaml.safe_load(git("show", f"{head}:{path}"))
    except yaml.YAMLError:
        return True
    if not isinstance(workflow, dict):
        return True
    for on in events(workflow):
        names = on if isinstance(on, (list, dict)) else [on]
        if any(name in ("push", "pull_request", "pull_request_target") for name in names):
            return True
    return False


def cancel(repo: str, head: str, paths: set[str], wait: float, step: float = 5.0) -> set[int]:
    cancelled, seen = set(), set()
    deadline = time.monotonic() + wait
    while True:
        live = []
        for run in api(f"repos/{repo}/actions/runs?head_sha={head}&per_page=100").get("workflow_runs", []):
            path = run["path"].split("@")[0]
            if path in paths:
                seen.add(path)
                if run["status"] != "completed" and run["id"] not in cancelled:
                    live.append(run["id"])
        cancelled |= {run for run in live if stop(repo, run)}
        if (seen >= paths and cancelled.issuperset(live)) or time.monotonic() >= deadline:
            return cancelled
        time.sleep(step)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--head", required=True)
    parser.add_argument("--repo")
    parser.add_argument("--wait", type=float, default=60.0)
    args = parser.parse_args(argv)
    found = refused(args.head, Path.cwd())
    for path, reasons in found.items():
        for reason in reasons:
            print(f"Refused {path}: {reason}.")
    if not found:
        print("Every workflow that exists only on this head queues on a shared hosted lane.")
        return 0
    fired = {path for path in found if fires_on_head(args.head, path)}
    if args.repo and fired:
        for run in sorted(cancel(args.repo, args.head, fired, args.wait)):
            print(f"Cancelled run {run}.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
