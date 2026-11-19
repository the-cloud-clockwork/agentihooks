from typing import Any

STARTS_PER_PASS = 8


def every_waiting_head(scheduler: Any, waiting: list[tuple[str, str]], now_ms: int) -> list[str]:
    actions: list[str] = []
    for name, raw in waiting:
        # ruleid: lab-release-starts-every-waiting-head
        actions.append(scheduler._start(name, raw, now_ms))
    return actions


def every_waiting_head_but_the_quiet(scheduler: Any, waiting: list[tuple[str, str]], now_ms: int) -> list[str]:
    actions: list[str] = []
    for name, raw in waiting:
        if not raw:
            continue
        try:
            # ruleid: lab-release-starts-every-waiting-head
            actions.append(scheduler._start(name, raw, now_ms))
        except ValueError as error:
            actions.append(f"cannot expand {name}: {error}")
    return actions


def breaks_on_something_else(scheduler: Any, waiting: list[tuple[str, str]], now_ms: int) -> list[str]:
    actions: list[str] = []
    for name, raw in waiting:
        # ruleid: lab-release-starts-every-waiting-head
        actions.append(scheduler._start(name, raw, now_ms))
        if name == "last":
            break
    return actions


def stops_at_the_bound(scheduler: Any, waiting: list[tuple[str, str]], now_ms: int) -> list[str]:
    actions: list[str] = []
    started = 0
    for name, raw in waiting:
        if not raw:
            continue
        started += 1
        try:
            # ok: lab-release-starts-every-waiting-head
            actions.append(scheduler._start(name, raw, now_ms))
        except ValueError as error:
            actions.append(f"cannot expand {name}: {error}")
        if started == STARTS_PER_PASS:
            break
    return actions


def checks_the_bound_first(scheduler: Any, waiting: list[tuple[str, str]], now_ms: int) -> list[str]:
    actions: list[str] = []
    started = 0
    for name, raw in waiting:
        if started >= 8:
            break
        started += 1
        # ok: lab-release-starts-every-waiting-head
        actions.append(scheduler._start(name, raw, now_ms))
    return actions
