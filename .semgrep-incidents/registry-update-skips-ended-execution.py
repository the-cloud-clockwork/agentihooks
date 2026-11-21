from dataclasses import replace
from typing import Any


def _apply_write(current: Any, written: Any) -> Any:
    # ruleid: registry-update-skips-ended-execution
    if current.state == "finished" and written.state != "ended":
        written = replace(written, state="finished")
    return written


def merge_onto_seat(current: Any, incoming: Any) -> Any:
    result = incoming
    # ruleid: registry-update-skips-ended-execution
    if current.state == "finished":
        result = replace(incoming, state="finished")
    return result


def _apply_write_guarded(current: Any, written: Any) -> Any:
    if current.state == "ended":
        return current
    # ok: registry-update-skips-ended-execution
    if current.state == "finished" and written.state != "ended":
        written = replace(written, state="finished")
    return written


def set_launch_guard(current: Any, updated: Any) -> Any:
    if current.state == "ended":
        return None
    # ok: registry-update-skips-ended-execution
    if current.state == "finished":
        return replace(updated, state="finished")
    return updated
