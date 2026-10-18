"""Deletes expired and superseded Actions artifacts in the owner's billed repositories.

GitHub bills artifact storage only for private and internal repositories, against one organisation limit. Later
runs read the newest artifact of a name on a long lived or open pull request branch, from its newest run or its
newest successful run, so both are kept at any age.
"""

import argparse
import json
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

LONG_LIVED = frozenset({"dev", "main", "master"})
RUNS_PER_WORKFLOW = 3
ATTEMPTS = 3
MB = 1000**2
# Docker build actions upload one record per build under a random name that no run reads back.
BUILD_RECORD = ".dockerbuild"


def _read(command: list[str], key: str | None) -> list[dict]:
    for attempt in range(ATTEMPTS):
        try:
            done = subprocess.run(
                [*command, "--jq", f".{key}[]" if key else ".[]"],
                capture_output=True,
                text=True,
                check=True,
                timeout=300,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            if attempt == ATTEMPTS - 1:
                raise
            time.sleep(attempt + 1)
        else:
            return [json.loads(line) for line in done.stdout.splitlines() if line]


def rows(path: str, key: str | None) -> list[dict]:
    return _read(["gh", "api", "--paginate", path], key)


def first_page(path: str, key: str) -> list[dict]:
    return _read(["gh", "api", path], key)


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
        for run in first_page(runs, "workflow_runs"):
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


def passed_runs(repo: str, branches: set[str]) -> set[int]:
    return {
        run["id"]
        for branch in sorted(branches)
        for run in first_page(
            f"repos/{repo}/actions/runs?branch={quote(branch)}&status=success&per_page=100", "workflow_runs"
        )
    }


def _branch(row: dict) -> str | None:
    return (row.get("workflow_run") or {}).get("head_branch")


def _run(row: dict) -> int | None:
    return (row.get("workflow_run") or {}).get("id")


def _created(row: dict) -> datetime:
    return datetime.fromisoformat(row["created_at"])


def _size(rows) -> int:
    return sum(row["size_in_bytes"] for row in rows if not row["expired"])


def plan(
    found: list[dict], branches: frozenset[str], passed: set[int], now: datetime, keep: timedelta
) -> tuple[list[dict], list[dict]]:
    live = sorted((row for row in found if not row["expired"]), key=_created)
    newest, newest_passed = {}, {}
    for row in live:
        if _branch(row) in branches and not row["name"].endswith(BUILD_RECORD):
            newest[(row["name"], _branch(row))] = row["id"]
            if _run(row) in passed:
                newest_passed[(row["name"], _branch(row))] = row["id"]
    kept = {*newest.values(), *newest_passed.values()}
    free = [row for row in live if row["id"] not in kept]
    old = [row for row in free if _created(row) < now - keep]
    young = [row for row in free if _created(row) >= now - keep]
    return old + [row for row in found if row["expired"]], young


def delete(repo: str, row: dict) -> bool:
    try:
        done = subprocess.run(
            ["gh", "api", "--method", "DELETE", f"repos/{repo}/actions/artifacts/{row['id']}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return False
    return done.returncode == 0 or "(HTTP 404)" in done.stderr


def _survey(args: argparse.Namespace, now: datetime) -> tuple[dict, list[str]]:
    plans, unread = {}, []
    for repo in billed(args.owner):
        name = repo["full_name"]
        try:
            found = artifacts(name)
            branches = kept_branches(repo)
            passed = passed_runs(name, branches & {_branch(row) for row in found})
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            unread.append(name)
            continue
        plans[name] = (found, *plan(found, branches, passed, now, timedelta(hours=args.keep_hours)))
    return plans, unread


def prune(args: argparse.Namespace, now: datetime) -> dict:
    plans, unread = _survey(args, now)
    doomed = [(name, row) for name, (_, old, _) in plans.items() for row in old]
    total = sum(_size(found) - _size(old) for found, old, _ in plans.values())
    ripe = sorted(
        (
            (name, row)
            for name, (_, _, young) in plans.items()
            for row in young
            if _created(row) < now - timedelta(hours=args.floor_hours)
        ),
        key=lambda item: _created(item[1]),
    )
    for name, row in ripe:
        if total <= args.limit_mb * MB:
            break
        doomed.append((name, row))
        total -= row["size_in_bytes"]
    ranked = sorted(doomed, key=lambda item: (item[1]["expired"], -item[1]["size_in_bytes"]))
    chosen = {(name, row["id"]) for name, row in ranked[: args.budget]}
    report = []
    for name, (found, _, _) in plans.items():
        mine = [row for row_name, row in doomed if row_name == name]
        picked = [row for row in mine if (name, row["id"]) in chosen]
        deleted = [row for row in picked if not args.apply or delete(name, row)]
        report.append(
            {
                "repository": name,
                "listed": len(found),
                "live_bytes": _size(found),
                "deleted": len(deleted),
                "deleted_bytes": _size(deleted),
                "failed": len(picked) - len(deleted),
                "after_bytes": _size(found) - _size(deleted),
                "pending": len(mine) - len(picked),
            }
        )
    return {"repositories": report, "unread": unread}


def verdict(result: dict, limit: int) -> tuple[list[str], int]:
    lines = [f"::error title=Artifacts not listed::{name} could not be listed" for name in result["unread"]]
    rows = result["repositories"]
    lines += [
        f"::error title=Artifact deletion failed::{row['failed']} deletions failed in {row['repository']}"
        for row in rows
        if row["failed"]
    ]
    kept = sum(row["after_bytes"] for row in rows)
    pending = sum(row["pending"] for row in rows)
    if kept > limit and pending:
        lines.append(
            f"::warning title=Pruning continues::billed repositories keep {kept / MB:.1f} MB with {pending} deletions"
            " left for the next run"
        )
    elif kept > limit:
        lines.append(
            f"::error title=Kept artifacts over the limit::billed repositories keep {kept / MB:.1f} MB that no rule may"
            " delete"
        )
    return lines, int(any(line.startswith("::error") for line in lines))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    parser.add_argument("--keep-hours", type=float, default=24)
    parser.add_argument("--floor-hours", type=float, default=6)
    parser.add_argument("--limit-mb", type=int, default=1000)
    parser.add_argument("--budget", type=int, default=1500)
    parser.add_argument("--report", type=Path, default=Path("storage-report.json"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    result = prune(args, datetime.now(UTC))
    args.report.write_text(json.dumps(result, indent=2) + "\n")
    lines, status = verdict(result, args.limit_mb * MB)
    for row in result["repositories"]:
        print(
            f"{row['repository']}: {row['listed']} listed, {row['live_bytes'] / MB:.1f} MB live, {row['deleted']} deleted"
            f" ({row['deleted_bytes'] / MB:.1f} MB), {row['after_bytes'] / MB:.1f} MB kept, {row['pending']} pending"
        )
    print("\n".join(lines))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
