import math
from typing import NamedTuple, NotRequired, TypedDict

from scripts.swarm import elapsed

RAISE_MS = 2 * elapsed.PASS_MS
REVIEW = "review"


class Review(NamedTuple):
    share: int
    harnesses: tuple[str, ...]


class Held(NamedTuple):
    seats: int
    limit: int


class PendingRaise(TypedDict):
    target: int | None
    ticks: int
    since: NotRequired[int]


class Decision(TypedDict):
    ceilings: dict[str, int]
    pending_raise: PendingRaise
    reason: str


def _held(
    live: dict[str, int], demand: dict[str, int], total: int, free: dict[str, int], review: Review | None
) -> Held | None:
    """The review seats held before the other lanes take room while a reviewer is live or a review task waits: the
    live reviewers, and while review tasks wait one seat more or the share of the total when larger, never past the
    waiting tasks or the free seats of the lane's harnesses."""
    present, waiting = live.get(REVIEW, 0), demand.get(REVIEW, 0)
    if review is None or not (present or waiting):
        return None
    limit = present + min(waiting, sum(free.get(harness, 0) for harness in review.harnesses))
    wanted = max(present + bool(waiting), math.ceil(total * review.share / 100))
    return Held(min(total, limit, wanted), limit)


def _ceilings(live: dict[str, int], demand: dict[str, int], total: int, held: Held | None = None) -> dict[str, int]:
    ceilings: dict[str, int] = {}
    room = total - (held.seats if held else 0)
    for lane in ("plan", "ci", "eng"):
        ceilings[lane] = min(room, live.get(lane, 0))
        room -= ceilings[lane]
    for lane in ("plan", "ci", "eng"):
        added = min(room, bool(demand.get(lane)))
        ceilings[lane] += added
        room -= added
    wants = {lane: live.get(lane, 0) + demand.get(lane, 0) for lane in ceilings}
    if held is not None:
        ceilings[REVIEW], wants[REVIEW] = held.seats, held.limit
    while room:
        ready = [lane for lane in ceilings if ceilings[lane] < wants[lane]]
        if not ready:
            break
        lane = min(ready, key=ceilings.get)
        ceilings[lane] += 1
        room -= 1
    return ceilings


def calculate(
    live: dict[str, int],
    free_seats: dict[str, int],
    host_room: int | None,
    demand: dict[str, int],
    previous: Decision | None = None,
    *,
    now_ms: int,
    review: Review | None = None,
) -> Decision:
    seats = sum(free_seats.values())
    target = min(50, sum(live.values()) + (seats if host_room is None else min(seats, host_room)))
    current = sum(previous["ceilings"].values()) if previous is not None else sum(live.values())
    pending: PendingRaise = {"target": None, "ticks": 0}
    total = target
    prior = previous["pending_raise"] if previous is not None else pending
    since = prior.get("since", elapsed.anchored(now_ms, prior["ticks"])) if prior["ticks"] else now_ms
    waits = not elapsed.reached(now_ms - since, RAISE_MS)
    if target > current:
        pending = {"target": target, "ticks": min(prior["ticks"] + 1, 3), "since": since}
        total = current if waits else min(target, current + 2)
        if total == target:
            pending = {"target": None, "ticks": 0}
    total = max(total, min(target, sum(live.values()) + sum(bool(count) for count in demand.values())))
    held = _held(live, demand, total, free_seats, review)
    ceilings = _ceilings(live, demand, total, held)
    harnesses = ", ".join(f"{harness} {seats}" for harness, seats in sorted(free_seats.items()))
    host_text = "unknown" if host_room is None else host_room
    allocated = sum(ceilings.values())
    reason = f"Quota seats {seats} ({harnesses}); host room {host_text}; ceiling {allocated} of {target}"
    if allocated < target and allocated >= sum(live.values()) + sum(demand.values()):
        ready = ", ".join(f"{lane} {demand.get(lane, 0)}" for lane in ("eng", "ci", "plan"))
        reason += f"; capped by ready work {ready}"
    elif target > total:
        reason += (
            f"; raise held at tick {pending['ticks']} of 3" if waits else "; raise held by the two seat per tick limit"
        )
    if held is not None and demand.get(REVIEW):
        reason += f"; review share holds {held.seats} seats for {demand[REVIEW]} waiting review tasks"
    decision: Decision = {
        "ceilings": ceilings,
        "pending_raise": pending,
        "reason": reason + ".",
    }
    return decision
