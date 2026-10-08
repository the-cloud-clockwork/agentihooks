from scripts.swarm import claim_order


def rows(*tasks):
    return {t["id"]: {"state": "open", **t} for t in tasks}


def test_depth_is_the_longest_chain_of_open_tasks_waiting_on_each():
    ledger = rows(
        {"id": "a"},
        {"id": "b", "depends_on": ["a"]},
        {"id": "c", "depends_on": ["b"]},
        {"id": "d", "depends_on": ["a"]},
        {"id": "e", "depends_on": ["a"], "state": "done"},
        {"id": "f", "depends_on": ["missing"]},
    )
    assert claim_order.depths(ledger) == {"a": 2, "b": 1, "c": 0, "d": 0, "e": 0, "f": 0}


def test_a_dependency_cycle_ends_without_looping():
    ledger = rows({"id": "a", "depends_on": ["b"]}, {"id": "b", "depends_on": ["a"]})
    assert claim_order.depths(ledger) == {"a": 1, "b": 0}


def test_key_is_rank_then_the_fast_clear_exception_then_depth():
    ledger = rows(
        {"id": "a", "rank": "high", "difficulty": "S", "phase": "p1"},
        {"id": "b", "depends_on": ["a"], "phase": "p1"},
        {"id": "c", "rank": "low", "difficulty": "M", "phase": "p2"},
    )
    key = claim_order.key(ledger)
    assert key(ledger["a"]) == (1, False, -1)
    assert key(ledger["b"]) == (2, True, 0)
    assert key(ledger["c"]) == (3, True, 0)


def test_rank_orders_before_depth_and_the_exception():
    ledger = rows(
        {"id": "deep"},
        {"id": "w1", "depends_on": ["deep"], "rank": "low"},
        {"id": "w2", "depends_on": ["w1"], "rank": "low"},
        {"id": "small", "difficulty": "S", "rank": "low"},
        {"id": "w3", "depends_on": ["small"], "rank": "low"},
        {"id": "urgent", "rank": "urgent"},
    )
    assert claim_order.ordered(ledger) == ["urgent", "deep", "small", "w1", "w2", "w3"]


def test_within_a_rank_the_longer_chain_goes_first_and_ledger_order_breaks_ties():
    ledger = rows(
        {"id": "leaf"},
        {"id": "one"},
        {"id": "two"},
        {"id": "x", "depends_on": ["one"], "rank": "low"},
        {"id": "y", "depends_on": ["two"], "rank": "low"},
        {"id": "z", "depends_on": ["y"], "rank": "low"},
        {"id": "tie"},
    )
    assert claim_order.ordered(ledger) == ["two", "one", "leaf", "tie", "y", "x", "z"]


def test_a_small_task_that_unblocks_another_goes_ahead_of_a_deeper_one():
    ledger = rows(
        {"id": "deep", "difficulty": "M"},
        {"id": "d1", "depends_on": ["deep"], "rank": "low"},
        {"id": "d2", "depends_on": ["d1"], "rank": "low"},
        {"id": "small", "difficulty": "S"},
        {"id": "s1", "depends_on": ["small"], "rank": "low"},
    )
    assert claim_order.ordered(ledger)[:2] == ["small", "deep"]


def test_a_small_task_that_is_the_last_open_task_of_its_phase_goes_first():
    ledger = rows(
        {"id": "big", "phase": "p1"},
        {"id": "other", "phase": "p1"},
        {"id": "last", "difficulty": "S", "phase": "p2"},
        {"id": "closed", "phase": "p2", "state": "done"},
    )
    assert claim_order.ordered(ledger) == ["last", "big", "other", "closed"]


def test_a_small_task_with_open_phase_work_and_nothing_waiting_keeps_its_place():
    ledger = rows(
        {"id": "first", "phase": "p1"},
        {"id": "small", "difficulty": "S", "phase": "p1"},
        {"id": "loose", "difficulty": "S"},
    )
    assert claim_order.ordered(ledger) == ["first", "small", "loose"]


def test_a_larger_task_never_takes_the_exception():
    ledger = rows(
        {"id": "first", "phase": "p1"},
        {"id": "medium", "difficulty": "M", "phase": "p2"},
        {"id": "w", "depends_on": ["medium"], "phase": "p1", "rank": "low"},
    )
    assert claim_order.ordered(ledger)[:2] == ["medium", "first"]
    ledger["medium"]["difficulty"] = "L"
    ledger["w"]["depends_on"] = []
    assert claim_order.ordered(ledger)[:2] == ["first", "medium"]


def test_phase_order_is_never_used():
    ledger = rows({"id": "late", "phase": "p9"}, {"id": "early", "phase": "p1"})
    assert claim_order.ordered(ledger) == ["late", "early"]
