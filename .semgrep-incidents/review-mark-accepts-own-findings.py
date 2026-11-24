from dataclasses import replace
from typing import Any


def _acknowledge(item: Any, by: str, answer: str) -> Any:
    return replace(item, answer=answer, read_by=(*item.read_by, by))


def mark_every_finding(found: Any, by: str, answer: str) -> tuple[Any, ...]:
    # ruleid: review-mark-accepts-own-findings
    return tuple(_acknowledge(item, by, answer) for item in found.findings)


def mark_others_only(found: Any, by: str, answer: str) -> tuple[Any, ...]:
    own = [item.id for item in found.findings if item.by == by]
    if own:
        raise ValueError(own)
    # ok: review-mark-accepts-own-findings
    return tuple(_acknowledge(item, by, answer) for item in found.findings)


def mark_one(item: Any, by: str, answer: str) -> Any:
    if item.by == by:
        raise ValueError(item.id)
    # ok: review-mark-accepts-own-findings
    return _acknowledge(item, by, answer)
