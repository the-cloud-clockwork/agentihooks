from collections.abc import Callable
from typing import Any

RAISED = "raised"


class _Steps:
    RAISED = "raised"


observe_steps = _Steps()


def held(call: Callable[[], list[str]], box: dict[str, Any]) -> list[str]:
    # ruleid: tick-held-exception-shuts-every-step
    if RAISED in box:
        return []
    try:
        return call()
    except Exception as exc:
        box[RAISED] = exc
        return []


def held_in(call: Callable[[], list[str]], found: dict[str, Any]) -> list[str]:
    # ruleid: tick-held-exception-shuts-every-step
    if "raised" in found:
        return []
    return call()


def shut(box: dict[str, Any]) -> bool:
    # ruleid: tick-held-exception-shuts-every-step
    if observe_steps.RAISED in box:
        return True
    return "finished" in box


def raise_held(box: dict[str, Any]) -> None:
    # ok: tick-held-exception-shuts-every-step
    if RAISED in box:
        raise box[RAISED]


def print_held(box: dict[str, Any]) -> None:
    # ok: tick-held-exception-shuts-every-step
    if "raised" in box:
        print(f"a slow lane step held {box['raised']!r}")


def fell(fallen: set[str], name: str, needs: frozenset[str]) -> bool:
    # ok: tick-held-exception-shuts-every-step
    if fallen.isdisjoint(needs):
        return False
    fallen.add(name)
    return True
