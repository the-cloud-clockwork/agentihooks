from collections.abc import Mapping

from scripts.tick.deadline import Deadline

SLOW_S = 59.0


def deadlines(live: Mapping[str, float], slow_s: float = SLOW_S) -> tuple[Deadline, ...]:
    # ruleid: tick-slow-lane-fixed-deadline
    fixed_default = Deadline.after(slow_s)
    # ruleid: tick-slow-lane-fixed-deadline
    fixed_constant = Deadline.after(SLOW_S)
    # ruleid: tick-slow-lane-fixed-deadline
    fixed_literal = Deadline.after(59.0)
    # ok: tick-slow-lane-fixed-deadline
    live_deadline = Deadline.after(sum(live.values()))
    return fixed_default, fixed_constant, fixed_literal, live_deadline
