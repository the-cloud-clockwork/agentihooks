from typing import Any


def deletes_before_it_starts(scheduler: Any, waiting: dict[str, str], now_ms: int) -> list[str]:
    actions: list[str] = []
    for name, raw in waiting.items():
        # ruleid: lab-release-takes-a-head-out-before-it-starts
        scheduler.client.hdel(scheduler.WAITING, name)
        actions.append(scheduler._start(name, raw, now_ms))
    return actions


def deletes_before_it_starts_through_a_branch(scheduler: Any, waiting: dict[str, str], now_ms: int) -> list[str]:
    actions: list[str] = []
    for name, raw in waiting.items():
        if not raw:
            continue
        # ruleid: lab-release-takes-a-head-out-before-it-starts
        scheduler.client.hdel(scheduler.WAITING, name)
        try:
            actions.append(scheduler._start(name, raw, now_ms))
        except ValueError as error:
            actions.append(f"cannot expand {name}: {error}")
    return actions


def starts_then_deletes(scheduler: Any, waiting: dict[str, str], now_ms: int) -> list[str]:
    actions: list[str] = []
    for name, raw in waiting.items():
        actions.append(scheduler._start(name, raw, now_ms))
        # ok: lab-release-takes-a-head-out-before-it-starts
        scheduler.client.hdel(scheduler.WAITING, name)
    return actions


def deletes_without_starting(scheduler: Any, waiting: dict[str, str], now_ms: int) -> list[str]:
    actions: list[str] = []
    for name, raw in waiting.items():
        if not raw:
            # ok: lab-release-takes-a-head-out-before-it-starts
            scheduler.client.hdel(scheduler.WAITING, name)
            continue
        actions.append(f"holding {name}")
    return actions
