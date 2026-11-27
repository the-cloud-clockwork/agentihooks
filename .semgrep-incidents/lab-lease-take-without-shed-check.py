from typing import Any


def takes_every_job(queue: Any, worker: str) -> list[str]:
    grants: list[str] = []
    for job in queue.ordered():
        # ruleid: lab-lease-take-without-shed-check
        if queue.claim(job, worker):
            grants.append(job.id)
    return grants


def stops_after_the_claim(queue: Any, worker: str, stop: int) -> list[str]:
    grants: list[str] = []
    for job in queue.ordered():
        # ruleid: lab-lease-take-without-shed-check
        if queue.claim(job, worker):
            grants.append(job.id)
        if job.rank >= stop:
            break
    return grants


def stops_at_a_shed_rank_before_the_claim(queue: Any, worker: str, stop: int) -> list[str]:
    grants: list[str] = []
    for job in queue.ordered():
        if job.rank >= stop:
            break
        # ok: lab-lease-take-without-shed-check
        if not queue.claim(job, worker):
            continue
        grants.append(job.id)
    return grants
