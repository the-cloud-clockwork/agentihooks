import os
from collections.abc import Callable
from functools import wraps


def skip_classifier_child(handler: Callable[[], None]) -> Callable[[], None]:
    @wraps(handler)
    def guarded() -> None:
        if os.environ.get("AGENTIHOOKS_CLASSIFIER_CHILD") == "1":
            return
        handler()

    return guarded
