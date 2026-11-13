"""Worker leases over the lab queue: long poll grants, heartbeats, completion and reaping."""

import hmac
import secrets
from collections.abc import Callable
from dataclasses import dataclass

from scripts.ci_lab.queue import Queue
from scripts.ci_lab.units import Unit

__all__ = ["REQUEUES", "Grant", "Leases"]

REQUEUES = 1


@dataclass(frozen=True)
class Grant:
    job: str
    token: str
    head: str
    unit: Unit
    slots: int
    expires_ms: int


@dataclass
class Leases:
    queue: Queue
    ttl_ms: int
    clock: Callable[[], int]
    sleep: Callable[[int], None]
    poll_ms: int

    def lease(self, worker: str, free_slots: int, capabilities: frozenset[str], wait_ms: int) -> list[Grant]:
        """Grant fitting pending jobs, polling until some are granted or the wait ends."""
        deadline = self.clock() + wait_ms
        while not (grants := self._take(worker, free_slots, capabilities)):
            left = deadline - self.clock()
            if left <= 0:
                return []
            self.sleep(min(self.poll_ms, left))
        return grants

    def _take(self, worker: str, free_slots: int, capabilities: frozenset[str]) -> list[Grant]:
        grants: list[Grant] = []
        for job_id in self.queue.pending():
            if free_slots <= 0:
                break
            job = self.queue.job(job_id)
            if job is None or job.requirement.slots > free_slots:
                continue
            if not job.requirement.capabilities <= capabilities or not self.queue.claim(job_id):
                continue
            token = secrets.token_hex(16)
            expires_ms = self.clock() + self.ttl_ms
            self.queue.lease(job_id, worker, token, expires_ms)
            grants.append(Grant(job_id, token, job.head, job.unit, job.requirement.slots, expires_ms))
            free_slots -= job.requirement.slots
        return grants

    def owns(self, job: str, token: str) -> bool:
        """Whether the token is the live lease token of the job."""
        stored = self.queue.token(job)
        return bool(stored) and hmac.compare_digest(stored.encode(), token.encode())

    def heartbeat(self, job: str, token: str) -> int | None:
        """Extend the lease and return its new expiry, or None when the token does not hold it."""
        if not self.owns(job, token):
            return None
        expires_ms = self.clock() + self.ttl_ms
        return expires_ms if self.queue.extend(job, expires_ms) else None

    def complete(self, job: str, token: str, outcome: str) -> bool:
        """Record the outcome of a job whose live lease the token holds."""
        if not self.owns(job, token) or not self.queue.release(job):
            return False
        self.queue.finish(job, "done", outcome)
        return True

    def reap(self, now_ms: int) -> list[tuple[str, str]]:
        """Requeue each expired lease once; a job that expires again is lost."""
        reaped: list[tuple[str, str]] = []
        for job in self.queue.expired(now_ms):
            if not self.queue.release(job):
                continue
            if self.queue.attempts(job) < REQUEUES:
                self.queue.requeue(job)
                reaped.append((job, "pending"))
            else:
                self.queue.finish(job, "lost", "lost")
                reaped.append((job, "lost"))
        return reaped
