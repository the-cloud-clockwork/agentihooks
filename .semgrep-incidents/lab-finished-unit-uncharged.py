from typing import Any

from scripts.ci_lab import metrics


def counted_only(scheduler: Any, job: Any, report: dict[str, Any]) -> None:
    if scheduler.queue.tally(job):
        # ruleid: lab-finished-unit-uncharged
        metrics.observe_unit(scheduler.registry, "promotion", job.unit.kind, job.requirement.slots, report)


def charged_when_budgeted(scheduler: Any, job: Any, report: dict[str, Any]) -> None:
    if scheduler.queue.tally(job):
        # ok: lab-finished-unit-uncharged
        metrics.observe_unit(scheduler.registry, "promotion", job.unit.kind, job.requirement.slots, report)
        if scheduler.spending is not None:
            scheduler.spending.charge(job.requirement.slots, float(report.get("seconds") or 0.0))


def always_charged(spending: Any, registry: Any, job: Any, report: dict[str, Any]) -> None:
    # ok: lab-finished-unit-uncharged
    metrics.observe_unit(registry, "promotion", job.unit.kind, job.requirement.slots, report)
    spending.charge(job.requirement.slots, float(report.get("seconds") or 0.0))
