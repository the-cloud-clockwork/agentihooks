from typing import TypedDict


class PendingRaise(TypedDict):
    target: int | None
    ticks: int


class Decision(TypedDict):
    ceilings: dict[str, int]
    pending_raise: PendingRaise
    reason: str


def calculate(
    live: dict[str, int],
    free_seats: dict[str, int],
    host_room: int | None,
    demand: dict[str, int],
    previous: Decision | None = None,
) -> Decision:
    seats = sum(free_seats.values())
    target = min(50, sum(live.values()) + (seats if host_room is None else min(seats, host_room)))
    current = sum(previous["ceilings"].values()) if previous is not None else sum(live.values())
    pending: PendingRaise = {"target": None, "ticks": 0}
    total = target
    if target > current:
        prior = previous["pending_raise"] if previous is not None else pending
        ticks = min(prior["ticks"] + 1, 3) if prior["target"] == target else 1
        pending = {"target": target, "ticks": ticks}
        total = current if ticks < 3 else min(target, current + 2)
        if total == target:
            pending = {"target": None, "ticks": 0}
    ceilings = {}
    room = total
    for lane in ("plan", "ci", "eng"):
        ceilings[lane] = min(room, live.get(lane, 0))
        room -= ceilings[lane]
    for lane in ("plan", "ci", "eng"):
        added = min(room, demand.get(lane, 0))
        ceilings[lane] += added
        room -= added
    ceilings["eng"] += room
    harnesses = ", ".join(f"{harness} {seats}" for harness, seats in sorted(free_seats.items()))
    room = "unknown" if host_room is None else host_room
    reason = f"Quota seats {seats} ({harnesses}); host room {room}; ceiling {total} of {target}"
    if target > total:
        reason += (
            f"; raise held at tick {pending['ticks']} of 3"
            if pending["ticks"] < 3
            else "; raise held by the two seat per tick limit"
        )
    return {
        "ceilings": ceilings,
        "pending_raise": pending,
        "reason": reason + ".",
    }
