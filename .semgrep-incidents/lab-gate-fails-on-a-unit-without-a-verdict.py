from collections.abc import Sequence
from typing import Any

PASSED = "passed"
UNGRADED = frozenset({"lost", "crash", "cannot_run", "dropped"})


def fails_on_every_unit_that_did_not_pass(slots: Sequence[Any]) -> str | None:
    if not all(slot.outcome for slot in slots):
        return None
    # ruleid: lab-gate-fails-on-a-unit-without-a-verdict
    return "success" if all(slot.outcome == PASSED for slot in slots) else "failure"


def fails_on_any_unit_that_did_not_pass(slots: Sequence[Any]) -> str:
    # ruleid: lab-gate-fails-on-a-unit-without-a-verdict
    return "failure" if any(slot.outcome != PASSED for slot in slots) else "success"


def fails_only_on_a_unit_that_failed(slots: Sequence[Any]) -> str | None:
    if not all(slot.outcome for slot in slots):
        return None
    if all(slot.outcome == PASSED for slot in slots):
        return "success"
    # ok: lab-gate-fails-on-a-unit-without-a-verdict
    return "neutral" if all(slot.outcome == PASSED or slot.outcome in UNGRADED for slot in slots) else "failure"


def counts_the_units_that_passed(slots: Sequence[Any]) -> int:
    # ok: lab-gate-fails-on-a-unit-without-a-verdict
    return sum(1 if slot.outcome == PASSED else 0 for slot in slots)
