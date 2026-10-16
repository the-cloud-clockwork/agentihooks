import argparse
import json
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


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def api(*args: str) -> dict:
    return json.loads(subprocess.run(["gh", "api", *args], capture_output=True, check=True).stdout or "{}")


def stop(repo: str, run: int) -> bool:
    argv = ["gh", "api", "-X", "POST", f"repos/{repo}/actions/runs/{run}/cancel"]
    return subprocess.run(argv, capture_output=True).returncode == 0


def branch_only(head: str, root: Path) -> list[str]:
    listed = git("ls-tree", "-z", "--name-only", f"{head}:{WORKFLOWS}").split("\0")
    return [
        f"{WORKFLOWS}/{name}"
        for name in listed
        if name.endswith((".yml", ".yaml")) and not (root / WORKFLOWS / name).exists()
    ]


def lane_problem(block: object) -> str | None:
    if not isinstance(block, dict) or " ".join(str(block.get("group")).split()) != GROUP:
        return "has no shared hosted lane concurrency group"
    if block.get("queue") != "max":
        return "queues on the lane without queue: max"
    if block.get("cancel-in-progress") not in (None, False):
        return "may cancel other runs on the shared lane"
    return None


def problems(workflow: object) -> list[str]:
    if not isinstance(workflow, dict):
        return ["is not a workflow mapping"]
    jobs = workflow.get("jobs") or {}
    if not isinstance(jobs, dict):
        return ["has no jobs mapping"]
    found = []
    for name, job in jobs.items():
        job = job if isinstance(job, dict) else {}
        problem = lane_problem(job.get("concurrency", workflow.get("concurrency")))
        if problem:
            found.append(f"job {name} {problem}")
    return found


def grade(text: str) -> list[str]:
    try:
        return problems(yaml.safe_load(text))
    except yaml.YAMLError:
        return ["does not parse as YAML"]


def refused(head: str, root: Path) -> dict[str, list[str]]:
    graded = {path: grade(git("show", f"{head}:{path}")) for path in branch_only(head, root)}
    return {path: found for path, found in graded.items() if found}


def cancel(repo: str, head: str, paths: set[str], wait: float, step: float = 5.0) -> set[int]:
    cancelled, seen = set(), set()
    deadline = time.monotonic() + wait
    while True:
        for run in api(f"repos/{repo}/actions/runs?head_sha={head}&per_page=100").get("workflow_runs", []):
            path = run["path"].split("@")[0]
            if path in paths and run["status"] != "completed":
                seen.add(path)
                if run["id"] not in cancelled and stop(repo, run["id"]):
                    cancelled.add(run["id"])
        if seen >= paths or time.monotonic() >= deadline:
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
    if args.repo:
        for run in sorted(cancel(args.repo, args.head, set(found), args.wait)):
            print(f"Cancelled run {run}.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
