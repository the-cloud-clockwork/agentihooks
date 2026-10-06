import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import cli as swarm_cli
from scripts.swarm import phase_planning, phase_state
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
    for tid, description in build:
        fields = {"task": tid, "title": f"Task {tid}", "lane": "eng", "phase": "p1", "territory": ["scripts/swarm"]}
        ledger.add_task(SLUG, {**fields, "description": description}, "planner@a1b2c3-0001")
    ids = ",".join(tid for tid, _ in build)
    ledger.update_task(SLUG, "plan-p1", {"state": "done", "proof": {"slice": ids}})


def test_an_auto_phase_gets_one_plan_task_over_two_ticks(env):
    store, ledger = env
    set_phase("p1", planning="auto")
    run(store, ledger)
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


def test_the_tick_that_adds_a_plan_task_does_not_tick_its_phase_done(env):
    store, ledger = env
    ledger.add_task(SLUG, {"task": "t1", "title": "Early work", "lane": "eng", "phase": "p1"}, "init-swarm")
    ledger.update_task(SLUG, "t1", {"state": "done"})
    set_phase("p1", planning="auto")
    actions = run(store, ledger)
    assert phase("p1")["done"] is False
    assert not any("phase p1 ticked" in a for a in actions)
    assert not any("is done" in i.text for i in items(store, MASTER_SEAT))


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
    assert "Task t2 has a description under 20 words." in comment["text"]
    assert "t1" not in comment["text"]
    [item] = [i for i in items(store, MASTER_SEAT) if "Review" in i.text]
    assert item.fyi is False and "Task t2 has a description under 20 words." in item.text
    assert phase_state.lifecycle(p1, state(SLUG)) == "in_review"


def test_a_clean_slice_says_so_in_its_comment(env):
    store, ledger = env
    set_phase("p1", planning="auto")
    run(store, ledger)
    slice_done(ledger, ("t1", DONE_WHEN))
    run(store, ledger)
    [comment] = [c for c in phase("p1")["comments"] if c["by"] == "swarm"]
    assert comment["text"] == "The slice check found no problems in the 1 task of this slice."


def test_a_long_problem_list_fits_the_comment_limit_and_the_item_carries_all(env):
    store, ledger = env
    set_phase("p1", planning="auto")
    run(store, ledger)
    slice_done(ledger, *[(f"t{i}", "Short.") for i in range(8)])
    run(store, ledger)
    [comment] = [c for c in phase("p1")["comments"] if c["by"] == "swarm"]
    assert len(comment["text"].split()) <= 50
    assert "more in the review item" in comment["text"]
    [item] = [i for i in items(store, MASTER_SEAT) if "Review" in i.text]
    assert all(f"Task t{i} has a description under 20 words." in item.text for i in range(8))


def test_manual_autonomy_raises_a_priority_for_the_operator_and_no_master_item(env):
    store, ledger = env
    store.update(SLUG, autonomy="manual")
    set_phase("p1", planning="auto")
    run(store, ledger)
    slice_done(ledger, ("t1", DONE_WHEN))
    run(store, ledger)
    assert [p["item"] for p in state(SLUG)["priorities"]] == ["phases/p1"]
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
    assert "recommendation" in item.text


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
    assert "approve" in item.text and "recommendation" not in item.text


def test_a_phase_already_reviewed_is_not_reviewed_again(env):
    store, ledger = env
    set_phase("p1", planning="auto")
    run(store, ledger)
    slice_done(ledger, ("t1", DONE_WHEN))
    review = {
        "op": "phase_review",
        "id": "rv",
        "by": "operator",
        "item": "phases/p1",
        "state": "sent_back",
        "rounds": 1,
    }
    assert core.sync(SLUG, ops=[review])[1] == []
    run(store, ledger)
    assert phase("p1")["review"]["state"] == "sent_back"
    assert not any(c["by"] == "swarm" for c in phase("p1")["comments"])


def test_the_pass_returns_what_it_did(env):
    store, ledger = env
    set_phase("p1", planning="auto")
    config = store.config(SLUG)
    actions = phase_planning.planning_pass(InboxStore(store.redis), store, SLUG, state(SLUG), ledger, config)
    assert actions == ["queued plan-p1 for phase p1"]
    slice_done(ledger, ("t1", DONE_WHEN))
    actions = phase_planning.planning_pass(InboxStore(store.redis), store, SLUG, state(SLUG), ledger, config)
    assert actions == ["opened the review of phase p1"]
    assert phase_planning.planning_pass(InboxStore(store.redis), store, SLUG, state(SLUG), ledger, config) == []
