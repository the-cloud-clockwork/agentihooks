import argparse
import json
from collections.abc import Callable

from scripts.ci_queue_retry import _api

REPO = "the-cloud-clockwork/agentihooks"
WATCHER = "queue-fail-fast"
GATE = "Gate — Required"
REBUILT = "queue group is no longer current"
FAILED_RUNS = '.workflow_runs[] | select(.conclusion != "success") | .id'


def classify(jobs: list[dict], notes: Callable[[int], str]) -> str | None:
    red = {job["name"]: job for job in jobs if job.get("conclusion") not in ("success", "skipped")}
    if set(red) != {WATCHER, GATE}:
        return None
    return "rebuild" if REBUILT in notes(red[WATCHER]["id"]) else "drop"


def count(repo: str, day: str, api: Callable[..., str]) -> dict:
    found = {"day": day, "drops": [], "rebuilds": []}

    def notes(job_id: int) -> str:
        return "\n".join(note["message"] for note in json.loads(api(f"repos/{repo}/check-runs/{job_id}/annotations")))

    runs = f"repos/{repo}/actions/workflows/test.yml/runs?event=merge_group&created={day}&per_page=100"
    for run_id in api("--paginate", runs, "--jq", FAILED_RUNS).split():
        listed = api("--paginate", f"repos/{repo}/actions/runs/{run_id}/jobs?per_page=100", "--jq", ".jobs[]")
        if kind := classify([json.loads(line) for line in listed.splitlines() if line], notes):
            found[f"{kind}s"].append(int(run_id))
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("day")
    parser.add_argument("--repo", default=REPO)
    args = parser.parse_args(argv)
    print(json.dumps(count(args.repo, args.day, _api)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
