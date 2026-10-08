from types import SimpleNamespace

import pytest

from hooks.classifier import Answer, DecisionResult
from scripts.doctor import priming
from scripts.inbox.store import InboxStore
from scripts.swarm import cli as swarm_cli
from scripts.swarm import ledger_client, phase_planning, phase_state, slice_screen
from scripts.swarm.store import RedisStore
from tests.doctor.test_doctor_cli import FileLedger, core, new_ledger, state
from tests.swarm.test_delivery import FakeHerdr
from tests.swarm.test_tick import FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")
SLUG = "planned"
MASTER_SEAT = f"master@{SLUG}"
DONE_WHEN = (
    "Add the parser for the new phase field and cover each case with a unit test. Done when the tests pass in CI."
)


@pytest.fixture
def env(monkeypatch, tmp_path):
    import fakeredis

    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    content = {
        "title": "Planned work",
        "overview": "o",
        "phases": [{"title": "Build", "description": "d"}, {"title": "Later", "description": "d"}],
    }
    assert new_ledger.create(SLUG, content)
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(swarm_cli, "connect", lambda: store)
    monkeypatch.setattr(swarm_cli, "LedgerClient", FileLedger)
    monkeypatch.setattr(swarm_cli, "HerdrRuntime", FakeRuntime)
    monkeypatch.setattr(swarm_cli.timer, "ensure", lambda binary: True)
    args = [SLUG, "create", "--repo", "/repo", "--max-eng-agents", "0", "--max-plan-agents", "0"]
    assert swarm_cli.main(args) == 0
    store.update(SLUG, state="running")
    return store, FileLedger()


def set_phase(pid, **fields):
    op = {"op": "phase_update", "id": f"set-{pid}-{len(fields)}", "by": "engineer", "item": f"phases/{pid}"}
    assert core.sync(SLUG, ops=[{**op, "fields": fields}])[1] == []


def run(store, ledger):
    return swarm_cli.run_tick(store, SLUG, ledger, FakeRuntime(), FakeHerdr({}))


def phase(pid):
    return next(p for p in state(SLUG)["phases"] if p["id"] == pid)


def tasks(pid):
    return [t for t in state(SLUG)["tasks"] if t.get("phase") == pid]


def items(store, address):
    return InboxStore(store.redis).inbox(address)


def slice_done(ledger, *build):
    ledger.update_task(SLUG, "plan-p1", {"state": "claimed", "claimed_by": "planner@a1b2c3-0001"})
    for tid, description in build:
        fields = {"task": tid, "title": f"Task {tid}", "lane": "eng", "phase": "p1", "territory": ["scripts/swarm"]}
        fields["plan_url"] = "https://github.com/acme/app/issues/1"
        ledger.add_task(SLUG, {**fields, "description": description}, "planner@a1b2c3-0001")
    ids = ",".join(tid for tid, _ in build)
    ledger.update_task(SLUG, "plan-p1", {"state": "done", "proof": {"slice": ids}})


def test_an_auto_phase_gets_one_plan_task_over_two_ticks(env):
    store, ledger = env
    set_phase("p1", planning="auto")
    assert "queued plan-p1 for phase p1" in run(store, ledger)
    run(store, ledger)
    [plan] = tasks("p1")
    assert (plan["id"], plan["lane"], plan["kind"], plan["state"]) == ("plan-p1", "plan", "plan", "open")
    assert plan["title"] == "Slice phase Build into tasks"
    assert "p1" in plan["description"] and "Build" in plan["description"]
    assert phase_state.lifecycle(phase("p1"), state(SLUG)) == "planning"
    told = [i for i in items(store, MASTER_SEAT) if "plan" in i.text and "Build" in i.text]
    assert len(told) == 1 and told[0].fyi is True


@pytest.mark.parametrize("swarm_state", ["running", "drained"])
def test_the_pass_acts_on_a_running_or_drained_swarm(env, swarm_state):
    store, ledger = env
    set_phase("p1", planning="auto")
    store.update(SLUG, state=swarm_state)
    run(store, ledger)
    assert [t["id"] for t in tasks("p1")] == ["plan-p1"]


@pytest.mark.parametrize("swarm_state", ["paused", "stopped"])
def test_a_swarm_that_is_not_running_queues_nothing(env, swarm_state):
    store, ledger = env
    set_phase("p1", planning="auto")
    store.update(SLUG, state=swarm_state)
    run(store, ledger)
    assert tasks("p1") == []


def test_manual_waiting_and_out_of_scope_phases_get_no_plan_task(env):
    store, ledger = env
    set_phase("p2", planning="auto", depends_on=["p1"])
    run(store, ledger)
    assert tasks("p1") == [] and tasks("p2") == []
    set_phase("p1", planning="auto")
    core.sync(SLUG, ops=[{"op": "set", "id": "oos", "by": "engineer", "path": "phases/p1/out_of_scope", "value": True}])
    run(store, ledger)
    assert tasks("p1") == []


def unplanned(store):
    return [i for i in items(store, MASTER_SEAT) if i.sender == "swarm" and "has no tasks" in i.text]


def test_a_manual_phase_without_tasks_raises_one_master_notice(env):
    store, ledger = env
    set_phase("p2", planning="auto", depends_on=["p1"])
    run(store, ledger)
    run(store, ledger)
    [notice] = unplanned(store)
    assert notice.text == (
        "Phase p1 Build has no tasks and is planned manually, so nothing builds it: "
        "add its tasks, or set its planning to auto so a planner slices it."
    )
    assert notice.fyi is False


def test_a_manual_phase_with_a_task_and_an_auto_phase_raise_no_notice(env):
    store, ledger = env
    ledger.add_task(SLUG, {"task": "t1", "title": "Task one", "lane": "eng", "phase": "p1"}, "master")
    set_phase("p2", planning="auto")
    actions = phase_planning.planning_pass(
        InboxStore(store.redis), store, SLUG, state(SLUG), ledger, store.config(SLUG)
    )
    assert actions == ["queued plan-p2 for phase p2"]
    assert unplanned(store) == []


def test_a_manual_phase_whose_only_task_is_out_of_scope_raises_the_notice(env):
    store, ledger = env
    ledger.add_task(SLUG, {"task": "t1", "title": "Task one", "lane": "eng", "phase": "p1"}, "master")
    core.sync(SLUG, ops=[{"op": "set", "id": "oos", "by": "engineer", "path": "tasks/t1/out_of_scope", "value": True}])
    set_phase("p2", planning="auto", depends_on=["p1"])
    actions = phase_planning.planning_pass(
        InboxStore(store.redis), store, SLUG, state(SLUG), ledger, store.config(SLUG)
    )
    assert actions == [f"told {MASTER_SEAT}: plan-unplanned:p1"]


def test_a_doctor_swarm_raises_no_notice_for_its_standing_phases(env):
    store, ledger = env
    store.update(SLUG, template=priming.TEMPLATE)
    actions = phase_planning.planning_pass(
        InboxStore(store.redis), store, SLUG, state(SLUG), ledger, store.config(SLUG)
    )
    assert actions == []


def test_an_approved_auto_phase_whose_slice_left_scope_raises_no_notice(env):
    store, ledger = env
    set_phase("p1", planning="auto")
    set_phase("p2", planning="auto", depends_on=["p1"])
    run(store, ledger)
    slice_done(ledger, ("t1", DONE_WHEN))
    for tid in ("plan-p1", "t1"):
        out = {"op": "set", "id": f"oos-{tid}", "by": "engineer", "path": f"tasks/{tid}/out_of_scope", "value": True}
        core.sync(SLUG, ops=[out])
    ledger.review_phase(SLUG, "p1", "approved", by="operator")
    assert phase_state.lifecycle(phase("p1"), state(SLUG)) == "building"
    actions = phase_planning.planning_pass(
        InboxStore(store.redis), store, SLUG, state(SLUG), ledger, store.config(SLUG)
    )
    assert actions == []


def test_the_tick_that_adds_a_plan_task_does_not_tick_its_phase_done(env):
    store, ledger = env
    ledger.add_task(SLUG, {"task": "t1", "title": "Early work", "lane": "eng", "phase": "p1"}, "init-swarm")
    ledger.update_task(SLUG, "t1", {"state": "done"})
    set_phase("p1", planning="auto")
    actions = run(store, ledger)
    assert phase("p1")["done"] is False
    assert not any("phase p1 ticked" in a for a in actions)
    assert not any("is done" in i.text for i in items(store, MASTER_SEAT))


def test_the_tick_that_finishes_a_phase_queues_the_plan_of_the_phase_waiting_on_it(env):
    store, ledger = env
    ledger.add_task(SLUG, {"task": "t1", "title": "Early work", "lane": "eng", "phase": "p1"}, "init-swarm")
    ledger.update_task(SLUG, "t1", {"state": "done"})
    set_phase("p2", planning="auto", depends_on=["p1"])
    actions = run(store, ledger)
    assert phase("p1")["done"] is True
    assert [t["id"] for t in tasks("p2")] == ["plan-p2"]
    assert "drained" not in actions and store.config(SLUG).state == "running"
    assert not any("no task left" in c["text"] for c in state(SLUG)["chat"])


def test_a_finished_slice_gets_its_check_comment_and_a_pending_review_once(env):
    store, ledger = env
    set_phase("p1", planning="auto")
    run(store, ledger)
    slice_done(ledger, ("t1", DONE_WHEN), ("t2", "Make it work."))
    run(store, ledger)
    run(store, ledger)
    p1 = phase("p1")
    assert p1["review"]["state"] == "pending" and p1["review"]["rounds"] == 0
    assert p1["review"]["by"] == "swarm"
    [comment] = [c for c in p1["comments"] if c["by"] == "swarm"]
    assert comment["text"] == "The slice check found these problems. Task t2 has a description under 20 words."
    [item] = [i for i in items(store, MASTER_SEAT) if "Review" in i.text]
    assert item.fyi is False
    assert item.text == (
        "Review the slice planned for phase p1 Build: approve it or send it back with a note. "
        "Task t2 has a description under 20 words."
    )
    assert phase_state.lifecycle(p1, state(SLUG)) == "in_review"


def test_a_clean_slice_says_so_in_its_comment(env):
    store, ledger = env
    set_phase("p1", planning="auto")
    run(store, ledger)
    slice_done(ledger, ("t1", DONE_WHEN), ("t2", DONE_WHEN))
    run(store, ledger)
    [comment] = [c for c in phase("p1")["comments"] if c["by"] == "swarm"]
    assert comment["text"] == "The slice check found no problems in the 2 tasks of this slice."
    [item] = [i for i in items(store, MASTER_SEAT) if "Review" in i.text]
    assert item.text.endswith("send it back with a note. The slice check found no problems.")
    assert phase_planning._comment([], 1) == "The slice check found no problems in the 1 task of this slice."


def test_a_long_problem_list_fits_the_comment_limit_and_the_item_carries_all(env):
    store, ledger = env
    set_phase("p1", planning="auto")
    run(store, ledger)
    slice_done(ledger, *[(f"t{i}", "Short.") for i in range(8)])
    run(store, ledger)
    [comment] = [c for c in phase("p1")["comments"] if c["by"] == "swarm"]
    shown = " ".join(f"Task t{i} has a description under 20 words." for i in range(4))
    assert comment["text"] == f"The slice check found these problems. {shown} And 4 more in the review item."
    assert len(comment["text"].split()) <= 50
    [item] = [i for i in items(store, MASTER_SEAT) if "Review" in i.text]
    assert item.text.endswith(" ".join(f"Task t{i} has a description under 20 words." for i in range(8)))


def test_manual_autonomy_raises_a_priority_for_the_operator_and_no_master_item(env):
    store, ledger = env
    store.update(SLUG, autonomy="manual")
    set_phase("p1", planning="auto")
    run(store, ledger)
    slice_done(ledger, ("t1", DONE_WHEN))
    run(store, ledger)
    assert [(p["item"], p["text"]) for p in state(SLUG)["priorities"]] == [
        ("phases/p1", "Approve the slice planned for this phase or send it back.")
    ]
    assert not any("Review" in i.text for i in items(store, MASTER_SEAT))


def test_assist_autonomy_asks_the_master_for_a_recommendation_and_the_operator_to_decide(env):
    store, ledger = env
    store.update(SLUG, autonomy="assist")
    set_phase("p1", planning="auto")
    run(store, ledger)
    slice_done(ledger, ("t1", DONE_WHEN))
    run(store, ledger)
    assert [p["item"] for p in state(SLUG)["priorities"]] == ["phases/p1"]
    [item] = [i for i in items(store, MASTER_SEAT) if "Review" in i.text]
    assert item.text == (
        "Review the slice planned for phase p1 Build: post your recommendation as a comment on the phase, "
        "the operator approves. The slice check found no problems."
    )


@pytest.mark.parametrize("autonomy", ["delegate", "full"])
def test_delegate_and_full_send_the_review_to_the_master_only(env, autonomy):
    store, ledger = env
    store.update(SLUG, autonomy=autonomy)
    set_phase("p1", planning="auto")
    run(store, ledger)
    slice_done(ledger, ("t1", DONE_WHEN))
    run(store, ledger)
    assert state(SLUG)["priorities"] == []
    [item] = [i for i in items(store, MASTER_SEAT) if "Review" in i.text]
    held = " Not approved automatically because the classifier did not answer." if autonomy == "full" else ""
    assert item.text == (
        "Review the slice planned for phase p1 Build: approve it or send it back with a note. "
        f"The slice check found no problems.{held}"
    )


def classify(monkeypatch, *pairs, calibrated=True, source="pplx-decider-v1-27b"):
    answers = {}
    for i, (score, confidence, p_yes) in enumerate(pairs):
        answers[f"size_{i}"] = Answer(type="score", score=score, confidence=confidence)
        answers[f"serves_{i}"] = Answer(type="noul", noul=p_yes)

    def fake(state, questions, **kwargs):
        return DecisionResult(answers, source, calibrated=calibrated)

    monkeypatch.setattr(slice_screen, "decide", fake)


def review_slice(store, ledger, autonomy, *build):
    store.update(SLUG, autonomy=autonomy)
    set_phase("p1", planning="auto")
    run(store, ledger)
    slice_done(ledger, *build)
    return run(store, ledger)


def swarm_comment():
    [comment] = [c for c in phase("p1")["comments"] if c["by"] == "swarm"]
    return comment["text"]


def review_items(store):
    return [i for i in items(store, MASTER_SEAT) if i.text.startswith("Review the slice")]


CLEAN = "The slice check found no problems in the 1 task of this slice."
ASK = "Review the slice planned for phase p1 Build: approve it or send it back with a note."
TOO_BIG = "Classifier: task t1 may be too big, several pull requests at confidence 0.90."
OFF_INTENT = "Classifier: task t1 may be off intent, serves the phase at probability 0.10."


def test_full_autonomy_approves_a_clean_slice_with_a_calibrated_answer(env, monkeypatch):
    store, ledger = env
    classify(monkeypatch, (1.0, 0.9, 0.9))
    actions = review_slice(store, ledger, "full", ("t1", DONE_WHEN))
    run(store, ledger)
    assert "approved the slice of phase p1" in actions
    review = phase("p1")["review"]
    assert (review["state"], review["by"]) == ("approved", "swarm")
    assert review["note"] == "The slice check and the classifier found nothing."
    assert phase_state.lifecycle(phase("p1"), state(SLUG)) == "building"
    assert swarm_comment() == f"{CLEAN} Full autonomy approved it."
    [item] = [i for i in items(store, MASTER_SEAT) if "approved the slice" in i.text]
    assert item.fyi is True
    assert item.text == (
        "For your information: the swarm approved the slice planned for phase p1 Build, "
        "the slice check and the classifier found nothing."
    )
    assert review_items(store) == [] and state(SLUG)["priorities"] == []


@pytest.mark.parametrize(
    ("answer", "source", "flag", "reason"),
    [
        ((2.0, 0.9, 0.9), "pplx-decider-v1-27b", TOO_BIG, "the classifier flagged a task"),
        ((1.0, 0.9, 0.1), "jev-1.13", OFF_INTENT, "the classifier flagged a task"),
        ((1.0, 0.9, 0.9), "haiku", "", "the answer came from the fallback haiku"),
    ],
)
def test_full_autonomy_leaves_a_flagged_or_fallback_slice_to_the_master(env, monkeypatch, answer, source, flag, reason):
    store, ledger = env
    classify(monkeypatch, answer, calibrated=source != "haiku", source=source)
    assert not any("approved" in a for a in review_slice(store, ledger, "full", ("t1", DONE_WHEN)))
    assert phase("p1")["review"]["state"] == "pending"
    tail = f"Not approved automatically because {reason}."
    assert swarm_comment() == " ".join(filter(None, [CLEAN, flag, tail]))
    [item] = review_items(store)
    assert item.text == f"{ASK} {flag or 'The slice check found no problems.'} {tail}"


def test_full_autonomy_never_approves_a_slice_the_check_faults(env, monkeypatch):
    store, ledger = env
    classify(monkeypatch, (1.0, 0.9, 0.9))
    review_slice(store, ledger, "full", ("t1", "Make it work."))
    assert phase("p1")["review"]["state"] == "pending"
    assert swarm_comment() == (
        "The slice check found these problems. Task t1 has a description under 20 words. "
        "Not approved automatically because the slice check found problems."
    )


def test_the_flag_confidence_comes_from_the_environment(env, monkeypatch):
    store, ledger = env
    monkeypatch.setenv("AGENTIHOOKS_PLAN_FLAG_CONFIDENCE", "0.95")
    classify(monkeypatch, (2.0, 0.9, 0.9))
    review_slice(store, ledger, "full", ("t1", DONE_WHEN))
    assert phase("p1")["review"]["state"] == "approved"


@pytest.mark.parametrize("autonomy", ["manual", "assist", "delegate"])
def test_flags_are_advisory_below_full_autonomy(env, monkeypatch, autonomy):
    store, ledger = env
    classify(monkeypatch, (2.0, 0.9, 0.1))
    review_slice(store, ledger, autonomy, ("t1", DONE_WHEN))
    assert phase("p1")["review"]["state"] == "pending"
    assert swarm_comment() == f"{CLEAN} {TOO_BIG} {OFF_INTENT}"
    for item in review_items(store):
        assert item.text.endswith(f"{TOO_BIG} {OFF_INTENT}")


def test_a_long_comment_keeps_the_reason_and_counts_what_it_cut(env, monkeypatch):
    store, ledger = env
    classify(monkeypatch, *[(3.0, 0.9, 0.1)] * 3)
    review_slice(store, ledger, "full", *[(f"t{i}", DONE_WHEN) for i in range(3)])
    text = swarm_comment()
    assert len(text.split()) <= 50
    assert text.endswith("more in the review item. Not approved automatically because the classifier flagged a task.")
    assert len(review_items(store)[0].text.split(" may ")) == 7


def send_back(note, by="operator"):
    op = {"op": "phase_review", "id": f"back-{note}", "by": by, "item": "phases/p1", "state": "sent_back", "note": note}
    core.check_op(op)
    assert core.sync(SLUG, ops=[op])[1] == []


def test_a_sent_back_slice_is_reviewed_again_once_the_planner_finishes(env):
    store, ledger = env
    set_phase("p1", planning="auto")
    run(store, ledger)
    slice_done(ledger, ("t1", DONE_WHEN))
    run(store, ledger)
    send_back("Split the parser task")
    run(store, ledger)
    assert phase_state.lifecycle(phase("p1"), state(SLUG)) == "planning"
    assert phase("p1")["review"]["state"] == "sent_back"
    ledger.update_task(SLUG, "plan-p1", {"state": "done"})
    run(store, ledger)
    review = phase("p1")["review"]
    assert (review["state"], review["rounds"], review["notes"]) == ("pending", 1, ["Split the parser task"])
    assert len([i for i in items(store, MASTER_SEAT) if i.text.startswith("Review the slice")]) == 2


def test_an_escalated_review_is_not_reopened(env):
    store, ledger = env
    set_phase("p1", planning="auto")
    run(store, ledger)
    slice_done(ledger, ("t1", DONE_WHEN))
    for n in range(3):
        run(store, ledger)
        send_back(f"Note {n}")
        ledger.update_task(SLUG, "plan-p1", {"state": "done"})
    assert phase("p1")["review"]["escalated"] is True
    ledger.update_task(SLUG, "plan-p1", {"state": "done"})
    run(store, ledger)
    review = phase("p1")["review"]
    assert (review["state"], review["rounds"], review["escalated"]) == ("sent_back", 3, True)
    assert len([i for i in items(store, MASTER_SEAT) if i.text.startswith("Review the slice")]) == 3


def test_the_pass_returns_what_it_did(env):
    store, ledger = env
    set_phase("p1", planning="auto")
    set_phase("p2", planning="auto", depends_on=["p1"])
    config = store.config(SLUG)
    actions = phase_planning.planning_pass(InboxStore(store.redis), store, SLUG, state(SLUG), ledger, config)
    assert actions == ["queued plan-p1 for phase p1"]
    slice_done(ledger, ("t1", DONE_WHEN))
    actions = phase_planning.planning_pass(InboxStore(store.redis), store, SLUG, state(SLUG), ledger, config)
    assert actions == ["opened the review of phase p1"]
    assert phase_planning.planning_pass(InboxStore(store.redis), store, SLUG, state(SLUG), ledger, config) == []


def test_two_phases_are_queued_and_reviewed_in_the_same_pass(env):
    store, ledger = env
    set_phase("p1", planning="auto")
    set_phase("p2", planning="auto")
    config = store.config(SLUG)
    actions = phase_planning.planning_pass(InboxStore(store.redis), store, SLUG, state(SLUG), ledger, config)
    assert actions == ["queued plan-p1 for phase p1", "queued plan-p2 for phase p2"]
    slice_done(ledger, ("t1", DONE_WHEN))
    fields = {"task": "t2", "title": "Task t2", "lane": "eng", "phase": "p2", "territory": ["scripts/swarm"]}
    fields["plan_url"] = "https://github.com/acme/app/issues/2"
    ledger.update_task(SLUG, "plan-p2", {"state": "claimed", "claimed_by": "planner@a1b2c3-0002"})
    ledger.add_task(SLUG, {**fields, "description": DONE_WHEN}, "planner@a1b2c3-0002")
    ledger.update_task(SLUG, "plan-p2", {"state": "done", "proof": {"slice": "t2"}})
    actions = phase_planning.planning_pass(InboxStore(store.redis), store, SLUG, state(SLUG), ledger, config)
    assert actions == ["opened the review of phase p1", "opened the review of phase p2"]


def test_a_task_id_the_ledger_would_refuse_in_a_comment_is_left_to_the_review_item(env):
    store, ledger = env
    set_phase("p1", planning="auto")
    run(store, ledger)
    fields = {"task": "fix_parser", "title": "Fix the parser", "lane": "eng", "phase": "p1", "territory": ["scripts"]}
    fields["plan_url"] = "https://github.com/acme/app/issues/1"
    ledger.update_task(SLUG, "plan-p1", {"state": "claimed", "claimed_by": "planner@a1b2c3-0001"})
    ledger.add_task(SLUG, {**fields, "description": "Short."}, "planner@a1b2c3-0001")
    slice_done(ledger, ("t2", "Short."))
    ledger.update_task(SLUG, "plan-p1", {"proof": {"slice": "fix_parser,t2"}})
    run(store, ledger)
    [comment] = [c for c in phase("p1")["comments"] if c["by"] == "swarm"]
    assert comment["text"] == (
        "The slice check found these problems. Task t2 has a description under 20 words. And 1 more in the review item."
    )
    [item] = [i for i in items(store, MASTER_SEAT) if "Review" in i.text]
    assert "Task fix_parser has a description under 20 words." in item.text


def test_a_phase_title_the_ledger_would_refuse_gets_a_plain_plan_title(env):
    store, ledger = env
    retitle = {"op": "phase_update", "id": "retitle", "by": "engineer", "item": "phases/p1"}
    assert core.sync(SLUG, ops=[{**retitle, "fields": {"title": "parse_fields", "planning": "auto"}}])[1] == []
    run(store, ledger)
    [plan] = tasks("p1")
    assert plan["title"] == "Slice this phase into tasks"
    assert "parse_fields" in plan["description"]


def test_a_problem_line_is_shown_only_while_the_comment_keeps_room_for_its_tail():
    head = "The slice check found these problems."
    fits, over = " ".join(["word"] * 37), " ".join(["word"] * 38)
    assert phase_planning._comment([fits], 1) == f"{head} {fits}"
    assert phase_planning._comment([over], 1) == f"{head} And 1 more in the review item."


def test_ledger_client_writes_a_phase_comment_and_a_review_record(monkeypatch):
    sent = []
    monkeypatch.setattr(
        ledger_client, "_ledger", lambda: SimpleNamespace(call=lambda slug, ops, service: sent.append(ops) or {})
    )
    client = ledger_client.LedgerClient()
    client.comment_phase("demo", "p1", "Slice checked.", "swarm")
    client.review_phase("demo", "p1", "pending")
    client.review_phase("demo", "p1", "sent_back", by="master@a1b2c3-0001", note="Split it")
    [[comment], [review], [back]] = sent
    assert {k: comment[k] for k in ("op", "by", "thread", "text")} == {
        "op": "add",
        "by": "swarm",
        "thread": "phases/p1/comments",
        "text": "Slice checked.",
    }
    assert {k: v for k, v in review.items() if k != "id"} == {
        "op": "phase_review",
        "by": "swarm",
        "item": "phases/p1",
        "state": "pending",
    }
    assert {k: v for k, v in back.items() if k != "id"} == {
        "op": "phase_review",
        "by": "master@a1b2c3-0001",
        "item": "phases/p1",
        "state": "sent_back",
        "note": "Split it",
    }


def test_ledger_client_sends_an_override_only_when_one_is_given(monkeypatch):
    sent = []
    monkeypatch.setattr(
        ledger_client, "_ledger", lambda: SimpleNamespace(call=lambda slug, ops, service: sent.append(ops) or {})
    )
    client = ledger_client.LedgerClient()
    override = {"reason": "Accepted", "problems": ["Task t1 names no territory."]}
    client.review_phase("demo", "p1", "approved", by="master@a1b2c3-0001", override=override)
    client.review_phase("demo", "p1", "approved", by="master@a1b2c3-0001", override=None)
    [[with_override], [without]] = sent
    assert {k: v for k, v in with_override.items() if k != "id"} == {
        "op": "phase_review",
        "by": "master@a1b2c3-0001",
        "item": "phases/p1",
        "state": "approved",
        "override": override,
    }
    assert "override" not in without


def build_task(ledger, tid, pid="p1", done=True):
    ledger.add_task(SLUG, {"task": tid, "title": f"Task {tid}", "lane": "eng", "phase": pid}, "init-swarm")
    if done:
        ledger.update_task(SLUG, tid, {"state": "done"})


def finish_release(ledger, pid="p1"):
    proof = {"command": "gh pr view", "output": "merged"}
    ledger.update_task(SLUG, f"release-{pid}", {"state": "done", "proof": proof})


def test_a_release_phase_gets_one_release_task_on_the_tick_its_build_tasks_are_done(env):
    store, ledger = env
    build_task(ledger, "t1")
    build_task(ledger, "t9", pid="p2", done=False)
    set_phase("p1", release=True)
    actions = run(store, ledger)
    assert phase("p1")["done"] is False
    assert [t["id"] for t in tasks("p1")] == ["t1", "release-p1"]
    run(store, ledger)
    [release] = [t for t in tasks("p1") if t["id"] != "t1"]
    assert (release["id"], release["lane"], release["kind"], release["state"]) == ("release-p1", "eng", "ops", "open")
    assert release["title"] == "Release phase Build"
    assert release["description"] == (
        "Release phase p1 Build: post the phase summary as a comment on the phase, and merge a changelog entry into "
        "dev in every repo the phase touched. Version bumps and the release dance stay with the operator."
    )
    assert release["contract"] == phase_planning.RELEASE_CONTRACT
    assert set(release["contract"]) == {"must", "check", "judge"}
    assert "added release-p1 for phase p1" in actions
    assert phase("p1")["done"] is False
    assert not any("phase p1 ticked" in a for a in actions)
    assert not any("is done" in i.text for i in items(store, MASTER_SEAT))


def test_no_release_task_while_a_build_task_is_open_or_without_the_release_field(env):
    store, ledger = env
    build_task(ledger, "t1")
    build_task(ledger, "t2", done=False)
    set_phase("p1", release=True)
    run(store, ledger)
    assert [t["id"] for t in tasks("p1")] == ["t1", "t2"]
    build_task(ledger, "t3", pid="p2")
    set_phase("p2", release=False)
    run(store, ledger)
    assert [t["id"] for t in tasks("p2")] == ["t3"]
    assert phase("p2")["done"] is True


def test_a_phase_without_tasks_or_done_or_out_of_scope_gets_no_release_task(env):
    store, ledger = env
    set_phase("p1", release=True)
    run(store, ledger)
    assert tasks("p1") == []
    set_phase("p1", release=False)
    build_task(ledger, "t1")
    run(store, ledger)
    assert phase("p1")["done"] is True
    set_phase("p1", release=True, title="Build")
    run(store, ledger)
    assert [t["id"] for t in tasks("p1")] == ["t1"]
    build_task(ledger, "t2", pid="p2")
    core.sync(SLUG, ops=[{"op": "set", "id": "oos", "by": "engineer", "path": "phases/p2/out_of_scope", "value": True}])
    set_phase("p2", release=True)
    run(store, ledger)
    assert [t["id"] for t in tasks("p2")] == ["t2"]


def test_a_waiting_phase_gets_no_release_task(env):
    store, ledger = env
    build_task(ledger, "t1", done=False)
    build_task(ledger, "t2", pid="p2")
    set_phase("p2", release=True, depends_on=["p1"])
    run(store, ledger)
    assert [t["id"] for t in tasks("p2")] == ["t2"]


def test_the_phase_ticks_after_its_release_task_and_the_dependent_phase_waits_for_it(env):
    store, ledger = env
    build_task(ledger, "t1")
    set_phase("p1", release=True)
    set_phase("p2", depends_on=["p1"])
    run(store, ledger)
    assert phase_state.lifecycle(phase("p2"), state(SLUG)) == "waiting"
    finish_release(ledger)
    actions = run(store, ledger)
    assert "phase p1 ticked" in actions
    assert not any("added release" in a for a in actions)
    assert phase("p1")["done"] is True
    assert phase_state.lifecycle(phase("p2"), state(SLUG)) == "building"
    assert [t["id"] for t in tasks("p1")] == ["t1", "release-p1"]


def test_an_out_of_scope_open_task_does_not_hold_the_release_task_back(env):
    store, ledger = env
    build_task(ledger, "t1")
    build_task(ledger, "t2", done=False)
    core.sync(
        SLUG, ops=[{"op": "set", "id": "oos-t2", "by": "engineer", "path": "tasks/t2/out_of_scope", "value": True}]
    )
    set_phase("p1", release=True)
    run(store, ledger)
    assert [t["id"] for t in tasks("p1")] == ["t1", "t2", "release-p1"]


def test_a_plan_task_and_a_release_task_are_added_in_the_same_pass(env):
    store, ledger = env
    set_phase("p1", planning="auto")
    build_task(ledger, "t2", pid="p2")
    set_phase("p2", release=True)
    actions = run(store, ledger)
    assert "queued plan-p1 for phase p1" in actions
    assert "added release-p2 for phase p2" in actions


def test_a_phase_title_the_ledger_would_refuse_gets_a_plain_release_title(env):
    store, ledger = env
    build_task(ledger, "t1")
    set_phase("p1", release=True, title="Build scripts/swarm/cli.py")
    run(store, ledger)
    [release] = [t for t in tasks("p1") if t["id"] == "release-p1"]
    assert release["title"] == "Release this phase"
