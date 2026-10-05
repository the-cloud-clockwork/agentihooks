import copy

from scripts.doctor import inbox
from tests.doctor.recorded import load

MIN = 60_000
WINDOW = 5 * MIN


def recorded():
    return load("inbox")


def ids(found):
    return sorted(f.id for f in found)


def by_id(items, item_id):
    return next(item for item in items if item["id"] == item_id)


def test_recorded_inbox_raises_its_open_item_its_escalation_and_its_bare_closes():
    rec = recorded()
    found = ids(inbox.findings(rec["items"], rec["now_ms"], WINDOW))
    assert found == sorted(
        [f"inbox-past-window/{item_id}" for item_id in rec["open"]]
        + [f"inbox-escalation/{item_id}" for item_id in rec["escalated"]]
        + [f"inbox-no-outcome/{item_id}" for item_id in rec["bare"]]
    )
    assert rec["open"] and rec["escalated"] and rec["bare"]


def test_planted_fault_an_item_left_open_past_its_window_is_raised():
    rec = recorded()
    items = copy.deepcopy(rec["items"])
    planted = by_id(items, rec["replied"])
    assert planted["id"] not in {f.subject for f in inbox.past_window(items, rec["now_ms"], WINDOW)}
    items = [planted]
    planted.update(state="pending", reason="")
    planted["history"] = planted["history"][:1]
    sent = planted["created_at"]
    assert inbox.past_window(items, sent + WINDOW - 1, WINDOW) == []
    [found] = inbox.past_window(items, sent + 3 * WINDOW, WINDOW)
    assert found.id == f"inbox-past-window/{planted['id']}"
    assert found.measure == 15
    assert f"from {planted['sender']} to {planted['address']}" in found.evidence


def test_planted_fault_an_escalated_item_names_each_step():
    rec = recorded()
    items = copy.deepcopy(rec["items"])
    planted = by_id(items, rec["replied"])
    assert inbox.escalated([planted]) == []
    planted["history"].insert(1, {"event": "escalated_operator", "by": "swarm", "reason": "shown", "at": 1})
    [found] = inbox.escalated([planted])
    assert found.id == f"inbox-escalation/{planted['id']}"
    assert "raised to the operator: shown" in found.evidence


def test_planted_fault_a_close_that_names_no_outcome_is_raised():
    rec = recorded()
    items = copy.deepcopy(rec["items"])
    planted = by_id(items, rec["replied"])
    assert inbox.no_outcome([planted]) == []
    planted.update(state="cancelled", reason="cancelled")
    [found] = inbox.no_outcome([planted])
    assert found.id == f"inbox-no-outcome/{planted['id']}"
    assert "closed cancelled with no outcome named" in found.evidence


def test_an_informational_item_closed_bare_is_not_raised_and_a_work_item_still_is():
    rec = recorded()
    work = copy.deepcopy(by_id(rec["items"], rec["replied"]))
    work.update(state="done", reason="done")
    fyi = {**copy.deepcopy(work), "id": "fyi1", "fyi": True}
    assert [f.id for f in inbox.no_outcome([work, fyi])] == [f"inbox-no-outcome/{work['id']}"]
