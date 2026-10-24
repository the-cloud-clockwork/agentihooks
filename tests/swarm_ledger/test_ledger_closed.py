import pytest

from scripts.swarm_ledger import ledger_closed, ledger_server
from tests.swarm_ledger.ledger_page import ledger_state

pytestmark = pytest.mark.unit


def task(index, done, **fields):
    return {"id": f"t{index}", "title": f"Task {index}", "state": "done" if done else "open", "done": done, **fields}


def test_done_means_done_flag_done_state_answered_question_or_done_alert():
    assert ledger_closed.closed("tasks", {"done": True}) is True
    assert ledger_closed.closed("tasks", {"state": "done"}) is True
    assert ledger_closed.closed("tasks", {"state": "pr"}) is False
    assert ledger_closed.closed("phases", {"done": False}) is False
    assert ledger_closed.closed("followups", {"done": True, "out_of_scope": True}) is False
    assert ledger_closed.closed("questions", {"answers": [{"id": "a", "text": "yes"}]}) is True
    assert ledger_closed.closed("questions", {"answers": [{"id": "a", "deleted": True}]}) is False
    assert ledger_closed.closed("questions", {"answers": []}) is False
    assert ledger_closed.closed("alerts", {"state": "done"}) is True
    assert ledger_closed.closed("alerts", {"state": "claimed", "done": True}) is False
    assert ledger_closed.closed("tasks", "t1") is False


def test_recent_lists_done_rows_newest_first():
    rows = [task(1, True), task(2, False), task(3, True), task(4, True)]
    assert [row["id"] for row in ledger_closed.recent("tasks", rows)] == ["t4", "t3", "t1"]
    questions = [{"id": "q1", "answers": [{"id": "a"}]}, {"id": "q2", "answers": []}]
    assert [row["id"] for row in ledger_closed.recent("questions", questions)] == ["q1"]


def test_the_view_keeps_open_rows_and_the_last_ten_done_in_order():
    rows = [task(n, n % 4 != 0) for n in range(1, 29)]
    view, hidden = ledger_closed.trimmed({"tasks": rows})
    done = [row["id"] for row in rows if row["done"]]
    kept = [row["id"] for row in view["tasks"]]
    assert kept == [row["id"] for row in rows if not row["done"] or row["id"] in done[-10:]]
    assert hidden == {
        "tasks": {
            "hidden": len(done) - 10,
            "kept": 10,
            "ids": done[:-10],
            "groups": {},
            "claimed_by": {},
            "plan_phases": [],
        }
    }


def test_ten_done_rows_or_fewer_leave_the_collection_whole():
    rows = [task(n, True) for n in range(10)] + [task(99, False)]
    assert ledger_closed.trimmed({"tasks": rows, "phases": [], "notes": [{"id": "n1"}]}) == ({}, {})


def test_eleven_done_rows_hide_exactly_the_oldest():
    rows = [task(n, True) for n in range(11)]
    view, hidden = ledger_closed.trimmed({"tasks": rows})
    assert view["tasks"] == rows[1:]
    assert hidden["tasks"]["ids"] == ["t0"]
    assert hidden["tasks"]["hidden"] == 1


def test_every_completable_collection_is_trimmed_and_others_are_left_alone():
    state = {
        "phases": [{"id": f"p{n}", "done": True} for n in range(12)],
        "questions": [{"id": f"q{n}", "answers": [{"id": "a", "text": "ok"}]} for n in range(13)],
        "followups": [{"id": f"f{n}", "done": True} for n in range(14)],
        "alerts": [{"id": f"a{n}", "state": "done"} for n in range(15)],
        "notes": [{"id": f"n{n}", "done": True} for n in range(20)],
        "priorities": [{"id": f"r{n}", "done": True} for n in range(20)],
    }
    view, hidden = ledger_closed.trimmed(state)
    assert {name: len(rows) for name, rows in view.items()} == {
        "phases": 10,
        "questions": 10,
        "followups": 10,
        "alerts": 10,
    }
    assert {name: entry["hidden"] for name, entry in hidden.items()} == {
        "phases": 2,
        "questions": 3,
        "followups": 4,
        "alerts": 5,
    }


def test_a_hidden_plan_task_names_its_phase_so_the_phase_still_reads_planned():
    rows = [task(n, True, kind="plan" if n in (0, 1) else "code", phase=f"p{n}") for n in range(12)]
    rows.append(task(50, True, kind="plan", phase="p50"))
    _, hidden = ledger_closed.trimmed({"tasks": rows})
    assert hidden["tasks"]["ids"] == ["t0", "t1", "t2"]
    assert hidden["tasks"]["plan_phases"] == ["p0", "p1"]


def test_the_page_view_carries_the_trimmed_rows_and_the_hidden_counts():
    rows = [task(n, True) for n in range(15)] + [task(20, False)]
    view = ledger_server.page_view(ledger_state({"tasks": rows, "phases": [], "freezes": []}), "", False)
    assert [row["id"] for row in view["tasks"]] == [f"t{n}" for n in range(5, 15)] + ["t20"]
    assert view["_meta"]["closed"]["tasks"]["hidden"] == 5
    assert view["_meta"]["closed"]["tasks"]["ids"] == [f"t{n}" for n in range(5)]


def test_the_page_view_of_a_small_ledger_adds_no_closed_entry():
    view = ledger_server.page_view(ledger_state({"tasks": [task(1, True)], "freezes": []}), "", False)
    assert "closed" not in view["_meta"]
    assert view["tasks"] == [task(1, True)]


def test_hidden_rows_count_per_plan_and_slice_and_keep_their_claimants():
    phases = [
        {"id": f"p{n}", "done": True, "plan": "plans/a" if n == 1 else "" if n == 0 else "plans/b"} for n in range(13)
    ]
    tasks = [task(n, True, slice="slices/s1" if n % 2 else "", claimed_by="eng" if n == 1 else "") for n in range(14)]
    _, hidden = ledger_closed.trimmed({"plans": [{"id": "a"}], "phases": phases, "tasks": tasks})
    assert hidden["phases"]["groups"] == {"unplanned": 2, "plans/a": 1}
    assert "claimed_by" not in hidden["phases"]
    assert hidden["tasks"]["groups"] == {"slices/s1": 2}
    assert hidden["tasks"]["claimed_by"] == {"t1": "eng"}
    questions = [{"id": f"q{n}", "answers": [{"id": "a"}]} for n in range(11)]
    assert "groups" not in ledger_closed.trimmed({"questions": questions})[1]["questions"]


def test_hidden_claims_read_the_claimants_from_the_view_meta():
    state = {"_meta": {"closed": {"tasks": {"claimed_by": {"t1": "eng", "t2": "ci"}}}}}
    assert ledger_closed.hidden_claims(state) == [{"id": "t1", "claimed_by": "eng"}, {"id": "t2", "claimed_by": "ci"}]
    assert ledger_closed.hidden_claims({"_meta": {}}) == []
    assert ledger_closed.hidden_claims({}) == []
