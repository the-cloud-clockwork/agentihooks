"""Deletes expired and superseded Actions artifacts in the owner's billed repositories.

GitHub bills artifact storage only for private and internal repositories. The newest artifact of a name on a
long lived branch or an open pull request branch is what later runs read, so it is kept at any age.
"""

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

LONG_LIVED = frozenset({"dev", "main", "master"})
RUNS_PER_WORKFLOW = 3
WORKERS = 4
MB = 1000**2


def rows(path: str, key: str | None, paginate: bool = True) -> list[dict]:
    command = ["gh", "api", path, "--jq", f".{key}[]" if key else ".[]"]
    if paginate:
        command.insert(2, "--paginate")
    done = subprocess.run(command, capture_output=True, text=True, check=True, timeout=900)
    return [json.loads(line) for line in done.stdout.splitlines() if line]


def billed(owner: str) -> list[dict]:
    found = rows(f"orgs/{owner}/repos?per_page=100", None)
    return sorted(
        (repo for repo in found if repo["visibility"] != "public" and not repo["archived"]),
        key=lambda repo: repo["full_name"],
    )


def by_name(repo: str) -> list[dict]:
    names = set()
    for workflow in rows(f"repos/{repo}/actions/workflows?per_page=100", "workflows"):
        runs = f"repos/{repo}/actions/workflows/{workflow['id']}/runs?status=success&per_page={RUNS_PER_WORKFLOW}"
        for run in rows(runs, "workflow_runs", paginate=False):
            names |= {
                row["name"]
                for row in rows(f"repos/{repo}/actions/runs/{run['id']}/artifacts?per_page=100", "artifacts")
            }
    return [
        row
        for name in sorted(names)
        for row in rows(f"repos/{repo}/actions/artifacts?name={quote(name)}&per_page=100", "artifacts")
    ]


def artifacts(repo: str) -> list[dict]:
    try:
        return rows(f"repos/{repo}/actions/artifacts?per_page=100", "artifacts")
    except subprocess.CalledProcessError:
        return by_name(repo)


def kept_branches(repo: dict) -> frozenset[str]:
    heads = {pull["head"]["ref"] for pull in rows(f"repos/{repo['full_name']}/pulls?state=open&per_page=100", None)}
    return LONG_LIVED | {repo["default_branch"]} | heads


def _branch(row: dict) -> str | None:
    return (row.get("workflow_run") or {}).get("head_branch")


def _created(row: dict) -> datetime:
    return datetime.fromisoformat(row["created_at"])


def plan(found: list[dict], branches: frozenset[str], now: datetime, keep: timedelta, floor: timedelta, limit: int):
    live = sorted((row for row in found if not row["expired"]), key=_created)
    newest = {(row["name"], _branch(row)): row["id"] for row in live if _branch(row) in branches}
    free = [row for row in live if row["id"] not in newest.values()]
    old = [row for row in free if _created(row) < now - keep]
    total = sum(row["size_in_bytes"] for row in live) - sum(row["size_in_bytes"] for row in old)
    over = []
    for row in free:
        if total <= limit or _created(row) >= now - floor:
            break
        if _created(row) >= now - keep:
            over.append(row)
            total -= row["size_in_bytes"]
    doomed = sorted(old + over, key=lambda row: row["size_in_bytes"], reverse=True)
    return doomed + [row for row in found if row["expired"]], total


def delete(repo: str, row: dict) -> bool:
    done = subprocess.run(
        ["gh", "api", "--method", "DELETE", f"repos/{repo}/actions/artifacts/{row['id']}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return done.returncode == 0 or "(HTTP 404)" in done.stderr


def prune(args, now: datetime) -> list[dict]:
    report = []
    budget = args.budget
    for repo in billed(args.owner):
        name = repo["full_name"]
        found = artifacts(name)
        doomed, after = plan(
            found,
            kept_branches(repo),
            now,
            timedelta(hours=args.keep_hours),
            timedelta(hours=args.floor_hours),
            args.limit_mb * MB,
        )
        chosen = doomed[:budget]
        if args.apply:
            with ThreadPoolExecutor(WORKERS) as pool:
                results = list(pool.map(lambda row: delete(name, row), chosen))
        else:
            results = [True] * len(chosen)
        deleted = [row for row, ok in zip(chosen, results, strict=True) if ok]
        budget -= len(chosen)
        left = sum(row["size_in_bytes"] for row in doomed[len(chosen) :] if not row["expired"])
        report.append(
            {
                "repository": name,
                "listed": len(found),
                "live_bytes": sum(row["size_in_bytes"] for row in found if not row["expired"]),
                "deleted": len(deleted),
                "deleted_bytes": sum(row["size_in_bytes"] for row in deleted),
                "failed": len(chosen) - len(deleted),
                "after_bytes": after + left,
                "pending": len(doomed) - len(chosen),
            }
        )
    return report


def verdict(report: list[dict], limit: int) -> tuple[list[str], int]:
    lines, status = [], 0
    for row in report:
        mb = row["after_bytes"] / MB
        if row["failed"]:
            lines.append(
                f"::error title=Artifact deletion failed::{row['failed']} deletions failed in {row['repository']}"
            )
            status = 1
        if row["after_bytes"] > limit and not row["pending"]:
            lines.append(
                f"::error title=Kept artifacts over the limit::{row['repository']} keeps {mb:.1f} MB that no rule may delete"
            )
            status = 1
        elif row["after_bytes"] > limit:
            lines.append(
                f"::warning title=Pruning continues::{row['repository']} keeps {mb:.1f} MB with {row['pending']} deletions"
                " left for the next run"
            )
    return lines, status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Delete expired and superseded artifacts in billed repositories.")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--keep-hours", type=float, default=24)
    parser.add_argument("--floor-hours", type=float, default=6)
    parser.add_argument("--limit-mb", type=int, default=300)
    parser.add_argument("--budget", type=int, default=1500)
    parser.add_argument("--report", type=Path, default=Path("storage-report.json"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    report = prune(args, datetime.now(UTC))
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    lines, status = verdict(report, args.limit_mb * MB)
    for row in report:
        print(
            f"{row['repository']}: {row['listed']} listed, {row['live_bytes'] / MB:.1f} MB live, {row['deleted']} deleted"
            f" ({row['deleted_bytes'] / MB:.1f} MB), {row['after_bytes'] / MB:.1f} MB kept, {row['pending']} pending"
        )
    print("\n".join(lines))
    return status


if __name__ == "__main__":
    sys.exit(main())
