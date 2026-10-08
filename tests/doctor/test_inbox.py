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


def test_past_window_separates_unread_delivery_from_receiver_backlog_and_labels_sent_age():
    row = copy.deepcopy(recorded()["items"][0])
    row.update(created_at=MIN, updated_at=10 * MIN, state="pending")
    row["history"] = [{"state": "pending", "at": MIN}]
    [unread] = inbox.past_window([row], 11 * MIN, WINDOW)
    row["state"] = "delivered"
    row["history"].append({"state": "delivered", "at": 10 * MIN})
    [delivered] = inbox.past_window([row], 11 * MIN, WINDOW)
    row["state"] = "read"
    [read] = inbox.past_window([row], 11 * MIN, WINDOW)
    assert unread.summary == "unread delivery; sent 10 minutes ago"
    assert delivered.summary == read.summary == "delivered backlog; sent 10 minutes ago"
    assert unread.id == delivered.id == read.id
    assert unread.measure == delivered.measure == read.measure == 10


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


def _delivered(address="eng-1@demo"):
    row = copy.deepcopy(recorded()["items"][0])
    row.update(created_at=MIN, updated_at=MIN, state="delivered", address=address)
    row["history"] = [{"state": "pending", "at": MIN}, {"state": "delivered", "at": MIN}]
    return row


def test_delivered_mail_to_a_live_receiver_working_it_is_not_backlog():
    row = _delivered()
    active = {row["address"]: inbox.Receiver(live=True, quiet_ms=WINDOW - 1)}
    assert inbox.past_window([row], 20 * MIN, WINDOW, active) == []
    later = {**_delivered("eng-2@demo"), "id": "later"}
    assert [f.subject for f in inbox.past_window([row, later], 20 * MIN, WINDOW, active)] == ["later"]


def test_delivered_mail_is_backlog_once_its_receiver_left_or_went_quiet():
    row = _delivered()
    quiet = {row["address"]: inbox.Receiver(live=True, quiet_ms=WINDOW)}
    left = {row["address"]: inbox.Receiver(live=False)}
    assert [f.subject for f in inbox.past_window([row], 20 * MIN, WINDOW, quiet)] == [row["id"]]
    assert [f.subject for f in inbox.past_window([row], 20 * MIN, WINDOW, left)] == [row["id"]]
    assert [f.subject for f in inbox.past_window([row], 20 * MIN, WINDOW)] == [row["id"]]


def test_unread_mail_keeps_the_window_while_its_receiver_is_live():
    row = _delivered()
    row.update(state="pending")
    row["history"] = row["history"][:1]
    active = {row["address"]: inbox.Receiver(live=True)}
    assert [f.subject for f in inbox.past_window([row], 20 * MIN, WINDOW, active)] == [row["id"]]


def test_mail_to_a_session_outside_the_swarm_is_left_out():
    row = _delivered("engineer-100001-0001-tmp-1")
    outside = {row["address"]: inbox.Receiver(scoped=False)}
    assert inbox.past_window([row], 20 * MIN, WINDOW, outside) == []
    assert inbox.findings([row], 20 * MIN, WINDOW, outside) == []


def test_delivered_seat_mail_is_backlog_once_the_agent_that_took_it_left_though_the_seat_has_a_live_occupant():
    row = _delivered()
    row["history"][1]["by"] = "engineer@a-0001"
    live = {row["address"]: inbox.Receiver(live=True), "engineer@a-0001": inbox.Receiver(live=False)}
    assert [f.subject for f in inbox.past_window([row], 20 * MIN, WINDOW, live)] == [row["id"]]
    taker = {**live, "engineer@a-0001": inbox.Receiver(live=True)}
    assert inbox.past_window([row], 20 * MIN, WINDOW, taker) == []


def test_the_reader_is_the_latest_agent_that_took_delivery_or_read_the_item():
    pending = {"state": "pending", "by": "swarm"}
    took = {"state": "delivered", "by": "engineer@a-0001"}
    assert inbox.reader({"history": [pending]}) == ""
    assert inbox.reader({"history": [pending, {"state": "delivered"}]}) == ""
    assert inbox.reader({"history": [pending, took, pending]}) == "engineer@a-0001"
    assert inbox.reader({"history": [pending, took, {"state": "read", "by": "engineer@a-0002"}]}) == "engineer@a-0002"
    assert inbox.reader({"history": [took, {"state": "delivered", "by": "engineer@a-0003"}]}) == "engineer@a-0003"
