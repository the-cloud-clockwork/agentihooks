from scripts.profiles import binding
from scripts.profiles.binding import _canary_process, process


class AncestorOnly:
    def validate(self, canary: str) -> int:
        # ruleid: codex-planner-canary-ancestor-only
        pid, _, _, _ = process()
        return pid


class SandboxLaunch:
    def validate(self, canary: str) -> int:
        # ok: codex-planner-canary-ancestor-only
        pid, _, _, _ = _canary_process()
        return pid


def open_turn() -> None:
    pass


def late_record(env: dict[str, str]) -> None:
    # ruleid: codex-planner-canary-recorded-late
    open_turn()
    binding.bind_sandbox(env, 23, 45, "thread")


def recorded_before_turn(env: dict[str, str]) -> None:
    binding.bind_sandbox(env, 23, 45, "thread")
    # ok: codex-planner-canary-recorded-late
    open_turn()


def replace(record: int, *, active_turn_id: None) -> int:
    return record


def late_nested_record(env: dict[str, str]) -> int:
    # ruleid: codex-planner-canary-recorded-late
    record = replace(0, active_turn_id=open_turn())
    binding.bind_sandbox(env, 23, 45, "thread")
    return record


def recorded_before_nested_turn(env: dict[str, str]) -> int:
    binding.bind_sandbox(env, 23, 45, "thread")
    # ok: codex-planner-canary-recorded-late
    record = replace(0, active_turn_id=open_turn())
    return record
