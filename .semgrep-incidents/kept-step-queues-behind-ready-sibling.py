from collections.abc import Mapping

from scripts.tick.engine import _kept_first
from scripts.tick.step import Step


def ready_in_site_order(pending: list[Step], names: set[str], records: Mapping[str, object]) -> list[Step]:
    # ruleid: kept-step-queues-behind-ready-sibling
    ready = [step for step in pending if step.needs & names <= records.keys()]
    return ready


def ready_kept_first(pending: list[Step], names: set[str], records: Mapping[str, object]) -> list[Step]:
    # ok: kept-step-queues-behind-ready-sibling
    ready = _kept_first(step for step in pending if step.needs & names <= records.keys())
    return ready


def waiting_in_order(pending: list[Step], names: set[str], records: Mapping[str, object]) -> list[Step]:
    # ok: kept-step-queues-behind-ready-sibling
    waiting = [step for step in pending if not step.needs & names <= records.keys()]
    return waiting
