from typing import Any

from scripts.ci_lab.scheduler import EVENTS, _needs, fold


def stale(state: Any) -> bool:
    # ruleid: lab-grade-required-frame
    return fold(EVENTS[state.tier], _needs(state), state.flags)


def matched(state: Any) -> bool:
    if state.required != ["lint"]:
        return False
    # ok: lab-grade-required-frame
    return fold(EVENTS[state.tier], _needs(state), state.flags)
