from scripts.tick.engine import SLOW
from scripts.tick.steps import CallStep


def lost_view() -> None:
    # ruleid: merge-settlement-dropped-by-budget
    CallStep("tick view", "wrapper view", None)


def lost_wait() -> None:
    # ruleid: merge-settlement-dropped-by-budget
    CallStep("waits end", "wrapper events", None)


def lost_merge() -> None:
    # ruleid: merge-settlement-dropped-by-budget
    CallStep("merge pass", "wrapper events", None)


def detached_wait() -> None:
    # ruleid: merge-settlement-detached-after-budget
    CallStep("waits end", "wrapper events", None, lane=SLOW, kept=True)


def detached_merge() -> None:
    # ruleid: merge-settlement-detached-after-budget
    CallStep("merge pass", "wrapper events", None, lane=SLOW, kept=True)


def kept_view() -> None:
    # ok: merge-settlement-dropped-by-budget
    CallStep("tick view", "wrapper view", None, kept=True)


def kept_wait() -> None:
    # ok: merge-settlement-dropped-by-budget, merge-settlement-detached-after-budget
    CallStep("waits end", "wrapper events", None, kept=True)


def kept_merge() -> None:
    # ok: merge-settlement-dropped-by-budget, merge-settlement-detached-after-budget
    CallStep("merge pass", "wrapper events", None, kept=True)
