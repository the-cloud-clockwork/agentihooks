from typing import Any


def apply(*args: Any, **kwargs: Any) -> dict:
    return {}


def request_bad(lift: bool) -> dict:
    # ruleid: merge-priority-swallowed-lift
    return apply(None, None, None, None, None, None)


def request_good(lift: bool) -> dict:
    # ok: merge-priority-swallowed-lift
    return apply(None, None, None, None, None, None, lift=lift)
