def sweep_each(ledger, slug, stale):
    for row, reason in stale:
        # ruleid: priority-clear-per-row
        ledger.clear_priority(slug, row["id"], reason)


def sweep_until_empty(ledger, slug, stale):
    while stale:
        row, reason = stale.pop()
        # ruleid: priority-clear-per-row
        ledger.clear_priority(slug, row["id"], reason)


def sweep_listed(ledger, slug, stale):
    # ruleid: priority-clear-per-row
    return [ledger.clear_priority(slug, row["id"], reason) for row, reason in stale]


def sweep_batched(ledger, slug, stale):
    # ok: priority-clear-per-row
    ledger.clear_priorities(slug, [(row["id"], reason) for row, reason in stale])


def resolve_one(ledger, slug, row, reason):
    # ok: priority-clear-per-row
    ledger.clear_priority(slug, row["id"], reason)
