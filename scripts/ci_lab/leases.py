"""Worker leases over the lab queue: long poll grants, heartbeats, completion and reaping."""

import hmac
import math
import secrets
from collections.abc import Callable, Collection
from dataclasses import dataclass, replace

from scripts.ci_lab.delivery import CLASSES
from scripts.ci_lab.outcomes import COMPLETED
from scripts.ci_lab.pods import PodImages, Pods, unmanaged, unread
from scripts.ci_lab.queue import Queue
from scripts.ci_lab.units import Unit

__all__ = ["REQUEUES", "SOLO", "Grant", "Leases", "Manifests", "release", "solo", "unstored"]

REQUEUES = 1

Manifests = Callable[[Unit], str]
Shed = Callable[[], Collection[str]]


def unstored(unit: Unit) -> str:
    """The manifests port where no object store holds batch manifests: a lease carries none."""
    return ""


def unshed() -> tuple[str, ...]:
    """The shed port where no daily core budget sheds a class: every pending job may lease."""
    return ()


SOLO = frozenset({("static", "type-gate")})


def solo(unit: Unit) -> bool:
    """Whether a worker runs one such unit at a time: a unit of a kind and job whose step fills gigabytes of memory,
    the job being what the unit's first member names before its first slash."""
    return bool(unit.members) and (unit.kind, unit.members[0].partition("/")[0]) in SOLO


@dataclass(frozen=True)
class Grant:
    job: str
    token: str
    head: str
    unit: Unit
    slots: int
    expires_ms: int
    base: str = ""
    grader: str = ""


@dataclass
class Leases:
    queue: Queue
    ttl_ms: int
    clock: Callable[[], int]
    sleep: Callable[[int], None]
    poll_ms: int
    pods: Pods = unmanaged
    manifests: Manifests = unstored
    image: str = ""
    images: PodImages = unread
    shed: Shed = unshed

    def stale(self, worker: str) -> bool:
        """Whether the worker's pod runs a floating image other than the one the lab wants."""
        _, _, wanted = self.image.partition("@")
        if not wanted:
            return False
        found = self.images(worker)
        if found is None or "@" in found.declared:
            return False
        return not found.running.endswith(f"@{wanted}")

    def lease(self, worker: str, free_slots: int, capabilities: frozenset[str], wait_ms: int) -> list[Grant]:
        """Grant fitting pending jobs, polling until some are granted or the wait ends; the worker's offer is its free
        slots while it waits and what its grants leave once it returns, and a poll that grants marks its pod busy. A
        worker that holds a solo unit is granted no other one, so its memory never holds two; the test reads the unit
        when the lease is taken, whenever the job was enqueued. No job of a class the shed port names is granted: a take
        stops at the first one, since every job after it in the pending order ranks at or past it."""
        self.queue.offer(worker, free_slots, self.clock())
        grants = self._poll(worker, free_slots, capabilities, wait_ms)
        self.queue.offer(worker, free_slots - sum(grant.slots for grant in grants), self.clock())
        if grants:
            self.pods(worker, True)
        return grants

    def _poll(self, worker: str, free_slots: int, capabilities: frozenset[str], wait_ms: int) -> list[Grant]:
        deadline = self.clock() + wait_ms
        while not (grants := self._take(worker, free_slots, capabilities)):
            left = deadline - self.clock()
            if left <= 0:
                return []
            self.sleep(min(self.poll_ms, left))
        return grants

    def _take(self, worker: str, free_slots: int, capabilities: frozenset[str]) -> list[Grant]:
        grants: list[Grant] = []
        if free_slots <= 0:
            return grants
        shed_from = min(map(CLASSES.index, self.shed()), default=math.inf)
        held: bool | None = None
        for job_id in self.queue.ordered():
            job = self.queue.job(job_id)
            if job is not None and job.rank >= shed_from:
                break
            if job is None or job.requirement.slots > free_slots:
                continue
            token = secrets.token_hex(16)
            now = self.clock()
            expires_ms = now + self.ttl_ms
            if not job.requirement.capabilities <= capabilities:
                continue
            if solo(job.unit):
                held = self._holds_solo(worker) if held is None else held
                if held:
                    continue
            unit = replace(job.unit, manifest=self.manifests(job.unit))
            if not self.queue.claim(job, worker, token, expires_ms, now):
                continue
            grants.append(Grant(job_id, token, job.head, unit, job.requirement.slots, expires_ms, job.base, job.grader))
            held = True if solo(job.unit) else held
            free_slots -= job.requirement.slots
            if free_slots <= 0:
                break
        return grants

    def _holds_solo(self, worker: str) -> bool:
        """Whether the worker holds a live lease on a solo unit, read from the records of every leased job."""
        leased = self.queue.jobs(self.queue.leased())
        return any(job is not None and job.worker == worker and solo(job.unit) for job in leased)

    def owns(self, job: str, token: str) -> bool:
        """Whether the token is the live lease token of the job."""
        stored = self.queue.token(job)
        return bool(stored) and hmac.compare_digest(stored.encode(), token.encode())

    def heartbeat(self, job: str, token: str) -> int | None:
        """Extend the lease and return its new expiry, or None when the token does not hold it."""
        expires_ms = self.clock() + self.ttl_ms
        return expires_ms if self.queue.extend(job, token, expires_ms) else None

    def complete(self, job: str, token: str, outcome: str) -> bool:
        """Record the outcome of a job whose live lease the token holds, refusing an outcome no worker reports."""
        if outcome not in COMPLETED:
            return False
        record = self.queue.job(job)
        if record is None or not self.queue.finish(record, token, outcome):
            return False
        release(self.queue, self.pods, record.worker)
        return True

    def reap(self, now_ms: int) -> list[tuple[str, str]]:
        """Requeue each expired lease once; a job that expires again is lost."""
        reaped: list[tuple[str, str]] = []
        for job in self.queue.expired(now_ms):
            record = self.queue.job(job)
            if record is not None and (state := self.queue.reap(record, now_ms, REQUEUES)):
                reaped.append((job, state))
                release(self.queue, self.pods, record.worker)
        return reaped


def release(queue: Queue, pods: Pods, worker: str) -> None:
    """Clear the deletion cost of the worker's pod once the worker holds no live lease, and mark it busy again when a
    grant landed while the clear was in flight."""
    if not queue.holds(worker):
        pods(worker, False)
        if queue.holds(worker):
            pods(worker, True)
