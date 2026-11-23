def missing_slices() -> tuple[str, ...]:
    # ruleid: tick-view-missing-plan-graph-parts
    LEDGER_PARTS = ("tasks", "_meta", "phases", "plans")
    return LEDGER_PARTS


def missing_plans() -> tuple[str, ...]:
    # ruleid: tick-view-missing-plan-graph-parts
    LEDGER_PARTS = ("tasks", "_meta", "phases", "slices")
    return LEDGER_PARTS


def missing_both() -> tuple[str, ...]:
    # ruleid: tick-view-missing-plan-graph-parts
    LEDGER_PARTS = ("tasks", "_meta", "phases")
    return LEDGER_PARTS


def complete() -> tuple[str, ...]:
    # ok: tick-view-missing-plan-graph-parts
    LEDGER_PARTS = ("tasks", "_meta", "phases", "slices", "plans")
    return LEDGER_PARTS
