import argparse
import json
import statistics
from pathlib import Path
from urllib.parse import quote

from scripts.ci_minutes import seconds
from scripts.ci_storage import rows


def measure(repo: str, count: int, until: str, since: str = "") -> dict:
    pulls = rows(f"repos/{repo}/pulls?state=closed&base=dev&sort=updated&direction=desc&per_page=100")
    merged = sorted(
        (pr for pr in pulls if pr["merged_at"] and since < pr["merged_at"] <= until),
        key=lambda pr: pr["merged_at"],
        reverse=True,
    )[:count]
    measured = []
    for pr in merged:
        branch = quote(pr["head"]["ref"], safe="")
        runs = rows(
            f"repos/{repo}/actions/workflows/mutation-preflight.yml/runs?branch={branch}&event=push&per_page=100",
            "workflow_runs",
        )
        runs = [run for run in runs if run["created_at"] <= pr["merged_at"]]
        stats = []
        for run in runs:
            jobs = rows(f"repos/{repo}/actions/runs/{run['id']}/jobs?filter=all&per_page=100", "jobs")
            for job in jobs:
                if job["name"].startswith("stats") and job["conclusion"] != "skipped":
                    stats.append(
                        {
                            "run": run["id"],
                            "head": run["head_sha"],
                            "job": job["id"],
                            "name": job["name"],
                            "seconds": max(0, seconds(job)),
                            "conclusion": job["conclusion"],
                        }
                    )
        measured.append(
            {
                "pr": pr["number"],
                "merged_at": pr["merged_at"],
                "branch": pr["head"]["ref"],
                "minutes": sum(job["seconds"] for job in stats) / 60,
                "jobs": stats,
            }
        )
    values = [pr["minutes"] for pr in measured]
    return {
        "count": len(measured),
        "requested": count,
        "until": until,
        "since": since,
        "mean": statistics.mean(values) if values else 0,
        "median": statistics.median(values) if values else 0,
        "total": sum(values),
        "pull_requests": measured,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--count", type=int, default=15)
    parser.add_argument("--until", required=True)
    parser.add_argument("--since", default="")
    parser.add_argument("--out", type=Path, default=Path("ci-stats-minutes.json"))
    args = parser.parse_args()
    result = measure(args.repo, args.count, args.until, args.since)
    args.out.write_text(json.dumps(result, indent=2))
    for pr in result["pull_requests"]:
        print(f"{pr['pr']}\t{pr['merged_at']}\t{len(pr['jobs'])}\t{pr['minutes']:.1f}", flush=True)
    print(json.dumps({key: value for key, value in result.items() if key != "pull_requests"}))


if __name__ == "__main__":
    main()
