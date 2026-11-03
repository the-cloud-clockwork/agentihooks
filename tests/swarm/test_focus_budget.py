import pytest


def work(name, *, lane="eng", state="open", hot=False, rank="normal"):
    return {"id": name, "lane": lane, "state": state, "kind": "hot" if hot else "code", "rank": rank}


def document(tasks):
    return {"tasks": tasks, "phases": [], "plans": [], "freezes": [{"verb": "focus", "target": "kind:hot"}]}


def test_scheduler_batch_fills_every_seat_with_focused_work_and_never_claims_free_or_frozen_work():
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig
    from scripts.swarm.tick import _spawn_order
    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    config = SwarmConfig("sw", "/repo", max_eng=10, max_ci=0, max_plan=0)
    store.create(config)
    hot = [work(f"hot{i}", hot=True) for i in range(12)]
    plain = [work(f"plain{i}", rank="urgent") for i in range(10)]
    frozen = work("frozen", hot=True, rank="urgent")
    ledger = FakeLedger([*plain, frozen, *hot])
    doc = {**ledger.state("sw"), "freezes": document([])["freezes"] + [{"verb": "freeze", "target": "tasks/frozen"}]}
    selected, _ = _spawn_order("sw", config, store, [], ledger.rows, doc, FakeRuntime())
    assert [task["id"] for _, task in selected] == [task["id"] for task in hot[:10]]


@pytest.mark.feature("swarm-focus")
def test_focused_tasks_take_the_whole_lane_before_free_tasks():
    from scripts.swarm import focus_budget

    hot = [work(f"hot{i}", hot=True) for i in range(3)]
    plain = [work(f"plain{i}") for i in range(10)]
    ready = [*plain, *hot]
    assert focus_budget.select(document(ready), ready, "eng", 5, running=[]) == hot + plain[:2]


@pytest.mark.parametrize("state", ["claimed", "pr"])
def test_running_work_of_the_lane_spends_its_seats(state):
    from scripts.swarm import focus_budget

    held = [work(f"held{i}", hot=True, state=state) for i in range(4)]
    hot = [work(f"hot{i}", hot=True) for i in range(3)]
    assert focus_budget.select(document(held + hot), hot, "eng", 6) == hot[:2]


def test_the_seats_are_counted_per_lane():
    from scripts.swarm import focus_budget

    held = [work(f"held{i}", hot=True, state="claimed") for i in range(5)]
    hot = work("ci", lane="ci", hot=True)
    plain = work("ci-normal", lane="ci")
    assert focus_budget.select(document(held + [hot, plain]), [plain, hot], "ci", 2) == [hot, plain]


def test_focused_tasks_follow_the_focus_queue_order():
    from scripts.swarm import focus_budget

    later, first = {**work("later"), "phase": "p2"}, {**work("first"), "phase": "p1"}
    found = {
        "tasks": [later, first],
        "phases": [{"id": "p1"}, {"id": "p2"}],
        "plans": [],
        "freezes": [{"verb": "focus", "target": "phases/p1"}, {"verb": "focus", "target": "phases/p2"}],
    }
    assert focus_budget.select(found, [later, first], "eng", 1, running=[]) == [first]


def test_refused_focused_tasks_do_not_hide_placeable_successors():
    from scripts.swarm import focus_budget

    refused, hot, plain = work("remote", hot=True), work("local", hot=True), work("plain")

    def place(tasks, room):
        return [task for task in tasks if task["id"] != "remote"][:room], []

    selected, _ = focus_budget.place(document([refused, hot, plain]), [refused, hot, plain], "eng", 2, [], place)
    assert selected == [hot, plain]


def test_unresolved_profiles_come_first():
    from scripts.swarm import focus_budget, profile_first

    hot, plain = work("hot", hot=True), work("plain")

    def place(tasks, room):
        return [{**task, profile_first.UNRESOLVED: "unknown"} if task == plain else task for task in tasks[:room]], []

    selected, _ = focus_budget.place(document([hot, plain]), [hot, plain], "eng", 10, [], place)
    assert selected == [{**plain, profile_first.UNRESOLVED: "unknown"}, hot]


def test_unresolved_profile_placement_stops_at_the_remaining_lane_room():
    from scripts.swarm import focus_budget, profile_first

    running = [work("running", state="claimed")]
    waiting = [work(f"waiting{i}") for i in range(3)]

    def locate(tasks, room):
        return [{**task, profile_first.UNRESOLVED: "unknown"} for task in tasks[:room]], []

    selected, notes = focus_budget.place(document(running + waiting), waiting, "eng", 3, running, locate)
    assert selected == [{**task, profile_first.UNRESOLVED: "unknown"} for task in waiting[:2]]
    assert notes == []


def test_unfocused_tasks_keep_their_existing_rank_order():
    from scripts.swarm import focus_budget

    plain = [work("urgent", rank="urgent"), work("normal")]
    assert focus_budget.select(document(plain), plain, "eng", 10) == plain


def test_a_document_without_freezes_ranks_every_task_unfocused():
    from scripts.swarm import focus_budget

    plain = [work("a"), work("b")]
    found = {"tasks": plain, "phases": [], "plans": []}
    assert focus_budget.ranks(found) == {"a": 0, "b": 0}
    assert focus_budget.select(found, plain, "eng", 10) == plain


def test_a_focus_on_a_plan_reaches_tasks_through_their_phase():
    from scripts.swarm import focus_budget

    inside, outside = {**work("inside"), "phase": "p1"}, work("outside")
    found = {
        "tasks": [outside, inside],
        "phases": [{"id": "p1", "plan": "plans/pl"}],
        "plans": [{"id": "pl"}],
        "freezes": [{"verb": "focus", "target": "plans/pl"}],
    }
    assert focus_budget.ranks(found) == {"outside": 1, "inside": 0}


def test_a_running_task_missing_from_the_document_still_spends_a_seat():
    from scripts.swarm import focus_budget

    hot, plain, ghost = work("hot", hot=True), work("plain"), work("ghost", state="claimed")
    assert focus_budget.select(document([hot, plain]), [plain, hot], "eng", 2, running=[ghost]) == [hot]
