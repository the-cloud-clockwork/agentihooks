"""Hold the claim of a task whose pull request is red only from checks failing on most other open pull requests."""

import json
import subprocess
import sys
from collections.abc import Callable

from scripts.swarm.ledger_events import RED

MIN_OTHERS = 3


class ReadError(Exception):
    pass


def failing(pull: dict) -> set[str]:
    return {
        check.get("name") or check.get("context")
        for check in pull.get("statusCheckRollup") or []
        if (check.get("conclusion") or check.get("state")) in RED
    }


def read(repo: str, run: Callable = subprocess.run) -> list[dict]:
    found = run(
        ["gh", "pr", "list", "--state", "open", "--limit", "200", "--json", "url,statusCheckRollup"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if found.returncode:
        raise ReadError(found.stderr.strip() or f"gh pr list exited {found.returncode}")
    return json.loads(found.stdout)


def shared(pulls: list[dict], url: str) -> set[str]:
    others = [failing(p) for p in pulls if p.get("url") != url and p.get("statusCheckRollup")]
    if len(others) < MIN_OTHERS:
        return set()
    mine = next((failing(p) for p in pulls if p.get("url") == url), set())
    return {name for name in mine if sum(name in other for other in others) * 2 > len(others)}


def held(pulls: list[dict], url: str) -> bool:
    mine = next((failing(p) for p in pulls if p.get("url") == url), set())
    return bool(mine) and mine <= shared(pulls, url)


def checker(repo: str, reader: Callable | None = None) -> Callable[[dict], bool]:
    known: list[dict] | None = None

    def admits(task: dict) -> bool:
        nonlocal known
        url = task.get("pr_url")
        if task.get("state") != "open" or not url:
            return True
        if known is None:
            try:
                known = (reader or read)(repo)
            except (ReadError, OSError, ValueError, subprocess.SubprocessError) as exc:
                print(f"fleet red hold skipped, listing open pull requests failed: {exc}", file=sys.stderr)
                known = []
        return not held(known, url)

    return admits
