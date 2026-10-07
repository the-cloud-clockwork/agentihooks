import json

import pytest

from scripts.swarm import cli as swarm_cli
from scripts.swarm import phase_state, plan_review, prompt
from scripts.swarm.store import AgentRecord
from tests.swarm.test_phase_planning import DONE_WHEN, SLUG, env, phase, run, set_phase, slice_done, state  # noqa: F401

pytestmark = pytest.mark.xdist_group("fakeredis")
MASTER = "master@a1b2c3-0001"
ENGINEER = "engineer@a1b2c3-0002"
BUTTONS = "use Approve plan or Send back on the phase in the ledger page"


@pytest.fixture
def review(env):  # noqa: F811
    store, ledger = env
    store.put_agent(SLUG, AgentRecord(name=MASTER, lane="master", task="master"))
    set_phase("p1", planning="auto")
    run(store, ledger)
    slice_done(ledger, ("t1", DONE_WHEN))
    run(store, ledger)
    assert phase("p1")["review"]["state"] == "pending"
    return store, ledger


def plan(capsys, autonomy_store, *argv, name=MASTER):
    code = swarm_cli.main([SLUG, "--as", name, "plan", *argv])
    out, err = capsys.readouterr()
    return code, json.loads(out) if code == 0 else err.strip()


def autonomy(store, level):
    store.update(SLUG, autonomy=level)


@pytest.mark.parametrize("level", ["manual", "assist"])
def test_the_operator_decides_at_manual_and_assist(review, capsys, level):
    store, _ = review
    autonomy(store, level)
    code, err = plan(capsys, store, "approve", "p1")
    advice = "; post your recommendation as a comment on the phase" if level == "assist" else ""
    assert (code, err) == (1, f"swarm: at {level} autonomy the operator decides this plan: {BUTTONS}{advice}")
    assert phase("p1")["review"]["state"] == "pending"


@pytest.mark.parametrize("level", ["delegate", "full"])
def test_the_master_approves_at_delegate_and_full_and_the_tick_claims_at_once(review, capsys, level):
    store, ledger = review
    autonomy(store, level)
    code, out = plan(capsys, store, "approve", "p1")
    assert (code, out) == (0, {"phase": "p1", "state": "approved", "rounds": 0, "problems": []})
    assert "override" not in phase("p1")["review"]
    assert (phase("p1")["review"]["by"], phase("p1")["review"]["note"]) == (MASTER, "")
    assert phase_state.lifecycle(phase("p1"), state(SLUG)) == "building"
    store.update(SLUG, max_eng=1)
    run(store, ledger)
    [t1] = [t for t in state(SLUG)["tasks"] if t["id"] == "t1"]
    assert t1["state"] == "claimed"


SHORT = "Task t1 has a description under 20 words."
REFUSED = (
    f"swarm: the slice check lists 1 problem for phase p1: {SHORT} "
    'Fix the slice, send it back, or approve with --override "<reason>".'
)


@pytest.fixture
def faulted(env):  # noqa: F811
    store, ledger = env
    store.put_agent(SLUG, AgentRecord(name=MASTER, lane="master", task="master"))
    set_phase("p1", planning="auto")
    run(store, ledger)
    slice_done(ledger, ("t1", "Too short"))
    run(store, ledger)
    assert phase("p1")["review"]["state"] == "pending"
    return store, ledger


def test_approve_refuses_a_slice_the_check_faults_and_names_each_problem(faulted, capsys):
    store, _ = faulted
    code, err = plan(capsys, store, "approve", "p1")
    assert (code, err) == (1, REFUSED)
    assert phase("p1")["review"]["state"] == "pending"


def test_two_problems_are_counted_in_the_plural(faulted, capsys):
    store, ledger = faulted
    ledger.update_task(SLUG, "t1", {"territory": []})
    code, err = plan(capsys, store, "approve", "p1")
    assert code == 1
    assert err.startswith(f"swarm: the slice check lists 2 problems for phase p1: {SHORT} Task t1 names no territory. ")


def test_an_override_approves_and_records_its_reason_and_problems_on_the_phase(faulted, capsys):
    store, _ = faulted
    reason = "The operator accepted the short description"
    code, out = plan(capsys, store, "approve", "p1", "--override", reason)
    assert (code, out) == (
        0,
        {"phase": "p1", "state": "approved", "rounds": 0, "problems": [SHORT], "override": reason},
    )
    review = phase("p1")["review"]
    assert (review["state"], review["by"]) == ("approved", MASTER)
    assert review["override"] == {"reason": reason, "problems": [SHORT]}


def test_a_blank_override_is_no_override(faulted, capsys):
    store, _ = faulted
    code, err = plan(capsys, store, "approve", "p1", "--override", "  ")
    assert (code, err) == (1, REFUSED)


def test_send_back_prints_the_problems(faulted, capsys):
    store, _ = faulted
    code, out = plan(capsys, store, "send-back", "p1", "--note", "Lengthen the task")
    assert (code, out["state"], out["problems"]) == (0, "sent_back", [SHORT])


def test_an_override_is_only_for_approve(faulted, capsys):
    with pytest.raises(SystemExit):
        swarm_cli.main([SLUG, "--as", MASTER, "plan", "send-back", "p1", "--note", "n", "--override", "r"])
    assert "--override" in capsys.readouterr().err


def test_a_plan_command_needs_its_action(review, capsys):
    with pytest.raises(SystemExit) as stopped:
        swarm_cli.main([SLUG, "--as", MASTER, "plan"])
    assert stopped.value.code == 2
    assert "approve" in capsys.readouterr().err


def test_a_phase_not_yet_planned_is_named_in_plain_words(review, capsys):
    store, _ = review
    set_phase("p2", planning="auto")
    code, err = plan(capsys, store, "approve", "p2")
    assert (code, err) == (1, "swarm: phase p2 is not in review; it is to plan")


def test_only_the_master_reviews_at_delegate(review, capsys):
    store, _ = review
    store.put_agent(SLUG, AgentRecord(name=ENGINEER, lane="eng", task="other"))
    code, err = plan(capsys, store, "approve", "p1", name=ENGINEER)
    assert code == 1 and "only the swarm master reviews a plan at delegate autonomy" in err


def test_send_back_reopens_the_plan_task_and_needs_a_note(review, capsys):
    store, ledger = review
    with pytest.raises(SystemExit):
        swarm_cli.main([SLUG, "--as", MASTER, "plan", "send-back", "p1"])
    capsys.readouterr()
    code, out = plan(capsys, store, "send-back", "p1", "--note", "Split the parser task")
    assert (code, out) == (0, {"phase": "p1", "state": "sent_back", "rounds": 1, "escalated": False, "problems": []})
    [plan_task] = [t for t in state(SLUG)["tasks"] if t["id"] == "plan-p1"]
    assert plan_task["state"] == "open"
    code, err = plan(capsys, store, "approve", "p1")
    assert code == 1 and "phase p1 is not in review; it is planning" in err


def test_the_third_send_back_hands_the_plan_to_the_operator(review, capsys):
    store, ledger = review
    for n in (1, 2):
        assert plan(capsys, store, "send-back", "p1", "--note", f"Note {n}")[0] == 0
        ledger.update_task(SLUG, "plan-p1", {"state": "done"})
        run(store, ledger)
    code, out = plan(capsys, store, "send-back", "p1", "--note", "Note 3")
    assert (code, out) == (0, {"phase": "p1", "state": "sent_back", "rounds": 3, "escalated": True, "problems": []})
    assert phase_state.lifecycle(phase("p1"), state(SLUG)) == "in_review"
    [ask] = [p for p in state(SLUG)["priorities"] if p["item"] == "phases/p1"]
    assert ask["text"] == "Decide the plan, sent back 3 times: Note 1; Note 2; Note 3"
    code, err = plan(capsys, store, "approve", "p1")
    assert code == 1
    assert f"the plan for phase p1 was sent back 3 times, so the operator decides: {BUTTONS}" in err


def test_an_unknown_phase_is_refused(review, capsys):
    store, _ = review
    code, err = plan(capsys, store, "approve", "p9")
    assert code == 1 and f"phase p9 is not in ledger {SLUG}" in err


@pytest.mark.parametrize(
    "level, words",
    [
        ("manual", "at manual autonomy the operator approves or sends it back from the ledger page"),
        ("assist", "post your recommendation as a comment on the phase"),
        ("delegate", "agentihooks swarm sw --as m plan approve <phase id>"),
        ("full", 'plan send-back <phase id> --note "<what to change>"'),
    ],
)
def test_master_prompt_names_the_review_rule(level, words):
    text = prompt.build_master("sw", "/repo", "m", {}, autonomy=level)
    assert words in text
    assert plan_review.review_line("agentihooks swarm sw --as m", level) in text


def test_review_lines_for_the_operator_levels():
    me = "agentihooks swarm sw --as m"
    assert plan_review.review_line(me, "manual") == (
        "- Review a phase plan in review only with a comment on the phase: at manual autonomy the operator "
        "approves or sends it back from the ledger page."
    )
    assert plan_review.review_line(me, "assist") == (
        "- When a phase plan is in review, post your recommendation as a comment on the phase: at assist autonomy "
        "the operator approves or sends it back from the ledger page."
    )
