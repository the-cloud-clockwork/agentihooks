from scripts.swarm_ledger import ledger_bin

IDLE = ledger_bin.IDLE_DAYS * ledger_bin.DAY_MS
OPEN = {"tasks": [{"id": "t1", "state": "open"}]}
DONE = {"tasks": [{"id": "t1", "state": "done"}]}


def ledger(base, **meta):
    return dict(base, _meta=meta)


def test_an_open_ledger_is_due_once_idle_past_the_window_from_its_last_change():
    now = 10 * IDLE
    assert ledger_bin.due(ledger(OPEN, updated_at=now - IDLE - 1, created_at=0), now)
    assert not ledger_bin.due(ledger(OPEN, updated_at=now - IDLE, created_at=0), now)
    assert not ledger_bin.due(ledger(OPEN, created_at=now - IDLE), now)
    assert ledger_bin.due(ledger(OPEN, created_at=now - IDLE - 1), now)


def test_a_ledger_without_times_counts_from_zero():
    assert ledger_bin.due(ledger(OPEN), IDLE + 1)
    assert ledger_bin.due({"tasks": OPEN["tasks"]}, IDLE + 1)


def test_a_finished_ledger_is_due_at_once():
    assert ledger_bin.due(ledger(DONE, updated_at=100), 101)


def test_a_restore_restarts_the_window_for_a_ledger_unchanged_since():
    now = 10 * IDLE
    state = ledger(DONE, updated_at=now - 2 * IDLE)
    assert not ledger_bin.due(state, now, restored_at=now - IDLE)
    assert ledger_bin.due(state, now, restored_at=now - IDLE - 1)
