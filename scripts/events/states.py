Table = dict[str, tuple[str, ...]]

TASK: Table = {
    "open": ("claimed",),
    "claimed": ("open", "blocked", "pr", "done"),
    "blocked": ("open", "claimed"),
    "pr": ("claimed", "open", "done"),
    "done": (),
}
EXECUTION: Table = {"starting": ("working", "ended"), "working": ("ended",), "ended": ()}
HIVE: Table = {
    "joining": ("live", "removed"),
    "live": ("stale", "draining", "removed"),
    "stale": ("live", "removed"),
    "draining": ("removed",),
    "removed": (),
}
SLOT: Table = {"reserved": ("occupied", "ended"), "occupied": ("ended",), "ended": ()}
MACHINES: dict[str, Table] = {"task": TASK, "execution": EXECUTION, "hive": HIVE, "slot": SLOT}


class IllegalTransition(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _refusal(machine: str, current: str, target: str) -> str | None:
    table = MACHINES.get(machine)
    if table is None:
        return f"unknown state machine {machine!r}"
    unknown = next((state for state in (current, target) if state not in table), None)
    if unknown is not None:
        return f"{machine} has no state {unknown!r}"
    if not table[current]:
        return f"{machine} state {current!r} is terminal"
    if target not in table[current]:
        return f"{machine} cannot go from {current!r} to {target!r}"
    return None


def transition(machine: str, current: str, target: str) -> str:
    reason = _refusal(machine, current, target)
    if reason is not None:
        raise IllegalTransition(reason)
    return target
