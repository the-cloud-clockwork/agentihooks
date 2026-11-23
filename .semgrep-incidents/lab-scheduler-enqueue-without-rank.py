from scripts.ci_lab.queue import Queue
from scripts.ci_lab.units import Unit


def enqueues_with_no_rank(queue: Queue, units: list[Unit], head: str, base: str, grader: str) -> list[str]:
    # ruleid: lab-scheduler-enqueue-without-rank
    return queue.enqueue(units, head, {}, base, grader)


def enqueues_under_the_grade_tier(queue: Queue, units: list[Unit], head: str, grader: str, tier: int) -> list[str]:
    # ok: lab-scheduler-enqueue-without-rank
    return queue.enqueue(units, head, {}, "", grader, tier)
