import os
from collections.abc import Callable
from functools import wraps


def _run_if_not_child(handler: Callable[[], None]) -> None:
    if os.environ.get("AGENTIHOOKS_CLASSIFIER_CHILD") == "1":
        return
    handler()


def skip_classifier_child(handler: Callable[[], None]) -> Callable[[], None]:
    @wraps(handler)
    def guarded() -> None:
        _run_if_not_child(handler)

    return guarded
