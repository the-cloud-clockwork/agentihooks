from typing import NotRequired, TypedDict


class PendingRaise(TypedDict):
    target: int | None
    ticks: int


class Decision(TypedDict):
    ceilings: dict[str, int]
    pending_raise: PendingRaise
    reason: str
    shift: NotRequired[int]


def _shifted(ceilings: dict[str, int], live: dict[str, int], demand: dict[str, int], shift: int) -> tuple[dict, str]:
    giver, taker = ("eng", "ci") if shift > 0 else ("ci", "eng")
    floor = live.get(giver, 0) + bool(demand.get(giver))
    needed = max(0, live.get(taker, 0) + demand.get(taker, 0) - ceilings[taker])
    moved = min(abs(shift), max(0, ceilings[giver] - floor), needed)
    ceilings[giver] -= moved
    ceilings[taker] += moved
    return {
        "shift": moved if giver == "eng" else -moved
    }, f"; lane shift {moved} from {giver} to {taker}" if moved else ""


def _ceilings(live: dict[str, int], demand: dict[str, int], total: int) -> dict[str, int]:
    ceilings = {}
    room = total
    for lane in ("plan", "ci", "eng"):
        ceilings[lane] = min(room, live.get(lane, 0))
        room -= ceilings[lane]
    for lane in ("plan", "ci", "eng"):
        added = min(room, bool(demand.get(lane)))
        ceilings[lane] += added
        room -= added
    while room:
        ready = [lane for lane in ceilings if ceilings[lane] < live.get(lane, 0) + demand.get(lane, 0)]
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
    shift: int = 0,
) -> Decision:
    seats = sum(free_seats.values())
    target = min(50, sum(live.values()) + (seats if host_room is None else min(seats, host_room)))
    current = sum(previous["ceilings"].values()) if previous is not None else sum(live.values())
    pending: PendingRaise = {"target": None, "ticks": 0}
    total = target
    if target > current:
        prior = previous["pending_raise"] if previous is not None else pending
        ticks = min(prior["ticks"] + 1, 3)
        pending = {"target": target, "ticks": ticks}
        total = current if ticks < 3 else min(target, current + 2)
        if total == target:
            pending = {"target": None, "ticks": 0}
    total = max(total, min(target, sum(live.values()) + sum(bool(count) for count in demand.values())))
    ceilings = _ceilings(live, demand, total)
    extra, shifted = _shifted(ceilings, live, demand, shift) if shift else ({}, "")
    harnesses = ", ".join(f"{harness} {seats}" for harness, seats in sorted(free_seats.items()))
    host_text = "unknown" if host_room is None else host_room
    allocated = sum(ceilings.values())
    reason = f"Quota seats {seats} ({harnesses}); host room {host_text}; ceiling {allocated} of {target}"
    if allocated < target and allocated >= sum(live.values()) + sum(demand.values()):
        ready = ", ".join(f"{lane} {demand.get(lane, 0)}" for lane in ("eng", "ci", "plan"))
        reason += f"; capped by ready work {ready}"
    elif target > total:
        reason += (
            f"; raise held at tick {pending['ticks']} of 3"
            if pending["ticks"] < 3
            else "; raise held by the two seat per tick limit"
        )
    reason += shifted
    decision: Decision = {
        "ceilings": ceilings,
        "pending_raise": pending,
        "reason": reason + ".",
        **extra,
    }
    return decision
