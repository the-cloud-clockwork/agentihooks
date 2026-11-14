from typing import Any


def started_only(agent: Any, outcome_at: int) -> int:
    # ruleid: quiet-signal-skips-launch
    return max(outcome_at, agent.started_at)


def launched_too(agent: Any, outcome_at: int) -> int:
    # ok: quiet-signal-skips-launch
    return max(outcome_at, agent.started_at, agent.launched_at)


def no_start(outcome_at: int, waited: int) -> int:
    # ok: quiet-signal-skips-launch
    return max(outcome_at, waited)
