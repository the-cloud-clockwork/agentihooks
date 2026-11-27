from typing import Any

from scripts.hive.orders import Ack


def failed_launch(runtime: Any, request: Any) -> Any:
    # ruleid: hive-failed-spawn-drops-pane
    outcome = runtime.spawn(request)
    if not outcome.ok:
        return Ack("failed", {}, outcome.detail)
    return outcome


def cleaned_launch(runtime: Any, request: Any, cleanup: Any) -> Any:
    # ok: hive-failed-spawn-drops-pane
    outcome = runtime.spawn(request)
    if not outcome.ok:
        return cleanup(outcome)
    return outcome
