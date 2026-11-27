from typing import Any

STARTS_PER_PASS = 8
LOOKED_AT_PER_PASS = 24


def only_looked_at_caps_the_pass(scheduler: Any, waiting: list[tuple[str, str]], now_ms: int) -> list[str]:
    actions: list[str] = []
    looked_at = 0
    for name, raw in waiting:
        if not raw:
            continue
        looked_at += 1
        try:
            # ruleid: lab-release-looked-at-cap-without-starts-cap
            actions.append(scheduler._start(name, raw, now_ms))
        except ValueError as error:
            actions.append(f"cannot expand {name}: {error}")
        if looked_at == LOOKED_AT_PER_PASS:
            break
    return actions


def both_caps_bound_the_pass(scheduler: Any, waiting: list[tuple[str, str]], now_ms: int) -> list[str]:
    actions: list[str] = []
    started = 0
    looked_at = 0
    for name, raw in waiting:
        if not raw:
            continue
        looked_at += 1
        try:
            # ok: lab-release-looked-at-cap-without-starts-cap
            actions.append(scheduler._start(name, raw, now_ms))
            started += 1
        except ValueError as error:
            actions.append(f"cannot expand {name}: {error}")
        if started == STARTS_PER_PASS or looked_at == LOOKED_AT_PER_PASS:
            break
    return actions
