"""Review rounds per swarm task: the third closes review and a fourth is refused."""

import fcntl
import hashlib
import json
import time
from pathlib import Path

from scripts.gates.budget import Budget
from scripts.gates.log import gates_dir
from scripts.swarm.store import SwarmError

ROUNDS = 3
CLOSED = (
    "Review is closed after three rounds: any finding still open gets one ruling from the other reader, "
    "then the merge proceeds."
)


def _counter(budget, task, pr_url, first_pr):
    if not pr_url:
        return "rounds-before-pr"
    path = budget.path(task, "first-pr")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        held.seek(0)
        first = held.read()
        if not first:
            first = first_pr or pr_url
            held.write(first)
    return "rounds" if pr_url == first else f"rounds-{hashlib.sha256(pr_url.encode()).hexdigest()}"


def counts(slug: str, task: str, home: Path | None = None) -> dict[str, int]:
    budget = Budget(slug, "reviews", home)
    return {path.name: budget.spent(task, path.name) for path in budget.path(task, "rounds").parent.glob("rounds*")}


def start(
    slug: str, task: str, home: Path | None = None, pr_url: str | None = None, first_pr: str = ""
) -> dict[str, str | int]:
    budget = Budget(slug, "reviews", home)
    counter = _counter(budget, task, pr_url, first_pr)
    allowed, spent = budget.spend(task, counter, ROUNDS)
    if not allowed:
        raise SwarmError(f"task {task} gets no fourth review round. {CLOSED}")
    log = gates_dir(slug, home) / "reviews" / "rounds.jsonl"
    with log.open("a") as stream:
        stream.write(
            json.dumps({"task": task, "counter": counter, "round": spent, "at": int(time.time() * 1000)}) + "\n"
        )
    return {"task": task, "round": spent, **({"notice": CLOSED} if spent == ROUNDS else {})}
