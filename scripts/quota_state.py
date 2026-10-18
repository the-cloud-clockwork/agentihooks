"""One account state vocabulary and drain rule for balance, swarm status and the ledger page."""

OPEN, DRAIN, CLOSED, UNKNOWN = "OPEN", "DRAIN", "CLOSED", "UNKNOWN"
DRAIN_LEFT = 10
BANDS = frozenset({"NORMAL", "REDUCE", "DRAIN_SOON", "DRAIN", "BLOCKED"})


def claude_left(five: float | None, week: float | None) -> float | None:
    return None if five is None or week is None else min(five, week)


def routing_left(harness: str, five: float | None, week: float | None) -> float | None:
    return week if harness == "codex" else claude_left(five, week)


def draining(left: float | None, limit: float = DRAIN_LEFT) -> bool:
    return left is not None and left <= limit


def state(cap: int | None, left: float | None) -> str:
    if cap is None:
        return UNKNOWN
    if cap == 0:
        return CLOSED
    return DRAIN if draining(left) else OPEN


def shown(band: str, cap: int | None, left: float | None) -> str:
    """A quota band becomes the shared state; a diagnostic state such as stale or signed out stays as it is."""
    return state(cap, left) if band in BANDS else band
