from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")
OUTCOMES = ("success", "captcha", "failure")


def run(current: Callable[[], T | None], browser: Callable[[], str], token: Callable[[], T]) -> T:
    found = current()
    if found is not None:
        return found
    outcome = browser()
    if outcome not in OUTCOMES:
        raise ValueError("browser outcome must be success, captcha or failure")
    if outcome == "success":
        found = current()
        if found is not None:
            return found
    return token()
