"""Review rounds per swarm task: the third closes review and a fourth is refused."""

from scripts.gates.budget import Budget
from scripts.swarm.store import SwarmError

ROUNDS = 3
CLOSED = (
    "Review is closed after three rounds: any finding still open gets one ruling from the other reader, "
    "then the merge proceeds."
)


def start(slug, task, home=None):
    allowed, spent = Budget(slug, "reviews", home).spend(task, "rounds", ROUNDS)
    if not allowed:
        raise SwarmError(f"task {task} gets no fourth review round. {CLOSED}")
    return {"task": task, "round": spent, **({"notice": CLOSED} if spent == ROUNDS else {})}
