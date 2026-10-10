from typing import NotRequired, TypedDict


class PendingRaise(TypedDict):
    target: int | None
    ticks: int


class Decision(TypedDict):
    ceilings: dict[str, int]
    pending_raise: PendingRaise
    reason: str
    shift: NotRequired[int]


def _shifted(ceilings: dict[str, int], live: dict[str, int], demand: dict[str, int], shift: int) -> int:
    giver, taker = ("eng", "ci") if shift > 0 else ("ci", "eng")
    floor = max(live.get(giver, 0), 1 if demand.get(giver, 0) else 0)
    moved = min(abs(shift), max(0, ceilings[giver] - floor))
    ceilings[giver] -= moved
    ceilings[taker] += moved
    return moved if shift > 0 else -moved


def calculate(
    live: dict[str, int],
    free_seats: dict[str, int],
    host_room: int | None,
    demand: dict[str, int],
    previous: Decision | None = None,
    shift: int = 0,
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
    moved = _shifted(ceilings, live, demand, shift)
    harnesses = ", ".join(f"{harness} {seats}" for harness, seats in sorted(free_seats.items()))
    host_text = "unknown" if host_room is None else host_room
    reason = f"Quota seats {seats} ({harnesses}); host room {host_text}; ceiling {total} of {target}"
    if target > total:
        reason += (
            f"; raise held at tick {pending['ticks']} of 3"
            if pending["ticks"] < 3
            else "; raise held by the two seat per tick limit"
        )
    if moved:
        reason += f"; lane shift {abs(moved)} from {'eng to ci' if moved > 0 else 'ci to eng'}"
    decision: Decision = {
        "ceilings": ceilings,
        "pending_raise": pending,
        "reason": reason + ".",
    }
    if shift:
        decision["shift"] = moved
    return decision
