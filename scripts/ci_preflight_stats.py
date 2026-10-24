import argparse
import json
import statistics
import subprocess
from datetime import datetime

PULLS = "pulls?state=closed&base=dev&per_page=100&sort=updated&direction=desc&page="
PREFLIGHT = "actions/workflows/mutation-preflight.yml/runs?event=push&per_page=100&branch="


def api(path: str):
    done = subprocess.run(["gh", "api", f"repos/{{owner}}/{{repo}}/{path}"], capture_output=True, check=True)
    return json.loads(done.stdout)


def when(text: str) -> datetime:
    return datetime.fromisoformat(text)


def merged(pages: int) -> list[dict]:
    rows = [row for page in range(1, pages + 1) for row in api(f"{PULLS}{page}") if row["merged_at"]]
    return sorted(rows, key=lambda row: row["merged_at"])


def first_commit(number: int) -> datetime:
    return min(when(row["commit"]["committer"]["date"]) for row in api(f"pulls/{number}/commits?per_page=100"))


def preflights(branch: str, start: datetime, end: datetime) -> list[tuple[int, str]]:
    found = []
    for run in api(f"{PREFLIGHT}{branch}")["workflow_runs"]:
        if start <= when(run["created_at"]) <= end:
            jobs = api(f"actions/runs/{run['id']}/jobs")["jobs"]
            found.append((run["id"], next((job["conclusion"] for job in jobs if job["name"] == "mutation"), "absent")))
    return found


def pick(pulls: list[dict], mode: str, at: datetime, count: int) -> list[tuple[dict, datetime]]:
    if mode == "before":
        chosen = [row for row in pulls if when(row["merged_at"]) < at][-count:]
        return [(row, first_commit(row["number"])) for row in chosen]
    picked = []
    for row in pulls:
        first = first_commit(row["number"])
        if first > at and len(picked) < count:
            picked.append((row, first))
    return picked


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["before", "after"])
    parser.add_argument("at")
    parser.add_argument("count", type=int)
    parser.add_argument("pages", type=int)
    args = parser.parse_args()
    runs, mutated, waits = [], [], []
    for row, first in pick(merged(args.pages), args.mode, when(args.at), args.count):
        found = preflights(row["head"]["ref"], first, when(row["merged_at"]))
        started = [run for run, mutation in found if mutation not in ("skipped", "absent")]
        wait = (when(row["created_at"]) - first).total_seconds() / 60
        runs.append(len(found))
        mutated.append(len(started))
        waits.append(wait)
        ids = " ".join(f"{run}:{mutation}" for run, mutation in found)
        print(
            f"pull request {row['number']} open after {wait:.1f} min, {len(found)} preflights, {len(started)} mutated: {ids}"
        )
    if not runs:
        print("no merged pull requests match")
        return 1
    print(f"pull requests {len(runs)}")
    print(f"median preflight runs per merged pull request {statistics.median(runs)}")
    print(f"median preflight runs that started the mutation job {statistics.median(mutated)}")
    print(f"median first commit to pull request open minutes {statistics.median(waits):.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
