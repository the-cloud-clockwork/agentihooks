"""The outcomes a lab unit ends with, named once for the worker, the leases, the retry policy and the scheduler.

A worker completes a lease with one of COMPLETED; the queue sets lost on an expired lease and the scheduler sets
dropped on a job a newer head removed, so neither is ever a completion. UNGRADED names the outcomes that end a unit
without a verdict: the store keeps none for them, a gate never fails on them and the queue starts such a job again.
"""

__all__ = [
    "CANNOT_RUN",
    "COMPLETED",
    "CRASH",
    "DROPPED",
    "FAILED",
    "GATE_FAILED",
    "LOSSES",
    "LOST",
    "PASSED",
    "SURVIVED",
    "UNGRADED",
]

PASSED = "passed"
FAILED = "failed"
GATE_FAILED = "gate_failed"
SURVIVED = "survived"
CRASH = "crash"
CANNOT_RUN = "cannot_run"
LOST = "lost"
DROPPED = "dropped"
LOSSES = frozenset({LOST, CRASH})
COMPLETED = frozenset({PASSED, FAILED, GATE_FAILED, SURVIVED, CRASH, CANNOT_RUN})
UNGRADED = frozenset({*LOSSES, CANNOT_RUN, DROPPED})
