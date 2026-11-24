from typing import Any


def unguarded() -> None:
    def _lease(leases: Any, worker: str, free_slots: int, capabilities: frozenset[str], wait_ms: int) -> list[Any]:
        # ruleid: lab-lease-handler-without-stale-check
        return leases.lease(worker, free_slots, capabilities, wait_ms)


def guarded() -> None:
    def _lease(leases: Any, worker: str, free_slots: int, capabilities: frozenset[str], wait_ms: int) -> list[Any]:
        if leases.stale(worker):
            return []
        # ok: lab-lease-handler-without-stale-check
        return leases.lease(worker, free_slots, capabilities, wait_ms)
