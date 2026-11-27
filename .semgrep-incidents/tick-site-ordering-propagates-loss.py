from typing import Any


def dropped_ordering(step: Any, dropped: set[str]) -> bool:
    # ruleid: tick-site-ordering-propagates-loss
    return bool(step.needs & dropped)


def gone_ordering(step: Any, gone: set[str]) -> bool:
    # ruleid: tick-site-ordering-propagates-loss
    return bool(step.needs & gone)


def cut_ordering(step: Any, cut: set[str]) -> bool:
    # ruleid: tick-site-ordering-propagates-loss
    return bool(step.needs & cut)


def fallen_ordering(step: Any, fallen: set[str]) -> bool:
    # ruleid: tick-site-ordering-propagates-loss
    return fallen.isdisjoint(step.needs)


def declared_failures(registry: Any, step: Any, dropped: set[str]) -> bool:
    # ok: tick-site-ordering-propagates-loss
    return bool(registry.failure_needs(step) & dropped)


def _after(found: Any, earlier: frozenset[str], timeout: float) -> bool:
    # ruleid: tick-site-handoff-propagates-ordering-loss
    return found.ended(earlier, timeout)


def declared_handoff(found: Any, registry: Any, step: Any, earlier: frozenset[str], timeout: float) -> bool:
    def _after() -> bool:
        # ok: tick-site-handoff-propagates-ordering-loss
        return found.ended(earlier, timeout, required=registry.failure_needs(step))

    return _after()
