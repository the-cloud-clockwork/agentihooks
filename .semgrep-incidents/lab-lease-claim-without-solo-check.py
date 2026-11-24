from typing import Any


def solo(unit: Any) -> bool:
    return bool(unit.members)


def takes_every_job(queue: Any, worker: str) -> list[str]:
    grants: list[str] = []
    for job in queue.ordered():
        # ruleid: lab-lease-claim-without-solo-check
        if queue.claim(job, worker):
            grants.append(job.id)
    return grants


def asks_after_the_claim(queue: Any, worker: str, held: bool) -> list[str]:
    grants: list[str] = []
    for job in queue.ordered():
        # ruleid: lab-lease-claim-without-solo-check
        if queue.claim(job, worker) and not (solo(job.unit) and held):
            grants.append(job.id)
    return grants


def skips_a_solo_unit_the_worker_holds(queue: Any, worker: str, held: bool) -> list[str]:
    grants: list[str] = []
    for job in queue.ordered():
        if solo(job.unit) and held:
            continue
        # ok: lab-lease-claim-without-solo-check
        if not queue.claim(job, worker):
            continue
        grants.append(job.id)
    return grants
