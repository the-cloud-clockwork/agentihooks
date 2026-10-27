import pytest


def work(name, *, lane="eng", state="open", hot=False, rank="normal"):
    return {"id": name, "lane": lane, "state": state, "kind": "hot" if hot else "code", "rank": rank}


def document(tasks):
    return {"tasks": tasks, "phases": [], "plans": [], "freezes": [{"verb": "focus", "target": "kind:hot"}]}


def test_scheduler_batch_keeps_five_focused_slots_and_never_claims_frozen_work():
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig
    from scripts.swarm.tick import _spawn_order
    from tests.swarm.test_tick import FakeLedger, FakeRuntime

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    config = SwarmConfig("sw", "/repo", max_eng=10, max_ci=0, max_plan=0)
    store.create(config)
    hot = [work(f"hot{i}", hot=True) for i in range(10)]
    plain = [work(f"plain{i}", rank="urgent") for i in range(10)]
    frozen = work("frozen", hot=True, rank="urgent")
    ledger = FakeLedger([*plain, frozen, *hot])
    doc = {**ledger.state("sw"), "freezes": document([])["freezes"] + [{"verb": "freeze", "target": "tasks/frozen"}]}
    selected, _ = _spawn_order("sw", config, store, [], ledger.rows, doc, FakeRuntime())
    ids = [task["id"] for _, task in selected]
    assert ids == [task["id"] for task in hot[:5] + plain[:5]]


def test_ten_lane_slots_allocate_five_focused_and_five_normal_tasks():
    from scripts.swarm import focus_budget

    hot = [work(f"hot{i}", hot=True) for i in range(10)]
    plain = [work(f"plain{i}") for i in range(10)]
    ready = [*plain, *hot]
    selected = focus_budget.select(document(ready), ready, "eng", 10, 50)[:10]
    assert selected == hot[:5] + plain[:5]


@pytest.mark.parametrize("state", ["claimed", "pr"])
def test_existing_focus_holders_leave_only_the_remaining_focus_slots(state):
    from scripts.swarm import focus_budget

    held = [work(f"held{i}", hot=True, state=state) for i in range(4)]
    held += [work(f"normal{i}", state=state) for i in range(4)]
    hot = [work(f"hot{i}", hot=True) for i in range(3)]
    plain = [work("normal")]
    assert focus_budget.select(document(held + hot + plain), plain + hot, "eng", 10, 50) == hot[:1] + plain


def test_focus_is_held_at_the_cap_even_if_normal_work_is_absent():
    from scripts.swarm import focus_budget

    held = [work(f"held{i}", hot=True, state="claimed") for i in range(5)]
    hot = work("waiting", hot=True, rank="urgent")
    assert focus_budget.select(document(held + [hot]), [hot], "eng", 10, 50) == []


def test_no_normal_work_means_no_new_focused_agents_at_fifty_percent():
    from scripts.swarm import focus_budget

    hot = [work(f"hot{i}", hot=True) for i in range(3)]
    assert focus_budget.select(document(hot), hot, "eng", 10, 50) == []


def test_focus_cap_is_independent_per_lane():
    from scripts.swarm import focus_budget

    held = [work(f"held{i}", hot=True, state="claimed") for i in range(5)]
    hot = work("ci", lane="ci", hot=True)
    plain = work("ci-normal", lane="ci")
    assert focus_budget.select(document(held + [hot, plain]), [plain, hot], "ci", 2, 50) == [hot, plain]


@pytest.mark.parametrize("capacity,weight,count", [(10, 0, 0), (10, 100, 10), (3, 50, 1), (1, 50, 0)])
def test_percentage_boundaries_use_integer_floor(capacity, weight, count):
    from scripts.swarm import focus_budget

    hot = [work(f"hot{i}", hot=True) for i in range(10)]
    normal = [work(f"plain{i}") for i in range(10)]
    selected = focus_budget.select(document(hot + normal), hot + normal, "eng", capacity, weight)
    assert [task for task in selected if task["kind"] == "hot"] == hot[:count]


def test_focused_pull_requests_without_live_owners_do_not_spend_agent_slots():
    from scripts.swarm import focus_budget

    held = [work(f"pr{i}", hot=True, state="pr") for i in range(5)]
    hot, plain = work("hot", hot=True), work("plain")
    assert focus_budget.select(document(held + [hot, plain]), [plain, hot], "eng", 10, 50, running=[]) == [hot, plain]


def test_refused_focused_tasks_do_not_hide_placeable_successors():
    from scripts.swarm import focus_budget

    refused, hot, plain = work("remote", hot=True), work("local", hot=True), work("plain")

    def place(tasks, room):
        return [task for task in tasks if task["id"] != "remote"][:room], []

    selected, _ = focus_budget.place(document([refused, hot, plain]), [refused, hot, plain], "eng", 2, 50, [], place)
    assert selected == [hot, plain]


def test_existing_normal_agents_support_new_focus_without_new_normal_work():
    from scripts.swarm import focus_budget

    normal = [work(f"running{i}", state="claimed") for i in range(4)]
    hot = [work(f"hot{i}", hot=True) for i in range(10)]
    assert focus_budget.select(document(normal + hot), hot, "eng", 10, 50) == hot[:4]


def test_unresolved_profiles_do_not_count_as_running_normal_work():
    from scripts.swarm import focus_budget, profile_first

    hot, plain = work("hot", hot=True), work("plain")

    def place(tasks, room):
        return [{**task, profile_first.UNRESOLVED: "unknown"} if task == plain else task for task in tasks[:room]], []

    selected, _ = focus_budget.place(document([hot, plain]), [hot, plain], "eng", 10, 50, [], place)
    assert selected == [{**plain, profile_first.UNRESOLVED: "unknown"}]


def test_unfocused_tasks_keep_their_existing_rank_order():
    from scripts.swarm import focus_budget

    plain = [work("urgent", rank="urgent"), work("normal")]
    assert focus_budget.select(document(plain), plain, "eng", 10, 50) == plain


def test_a_document_without_freezes_ranks_every_task_unfocused():
    from scripts.swarm import focus_budget

    plain = [work("a"), work("b")]
    found = {"tasks": plain, "phases": [], "plans": []}
    assert focus_budget.ranks(found) == {"a": 0, "b": 0}
    assert focus_budget.select(found, plain, "eng", 10, 50) == plain


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


def test_focused_pull_requests_hold_focus_slots_when_running_is_read_from_the_document():
    from scripts.swarm import focus_budget

    held = [work(f"pr{i}", hot=True, state="pr") for i in range(5)]
    hot, plain = work("hot", hot=True), [work(f"plain{i}") for i in range(5)]
    assert focus_budget.select(document([*held, hot, *plain]), [*plain, hot], "eng", 10, 50) == plain


def test_a_running_task_missing_from_the_document_counts_as_unfocused():
    from scripts.swarm import focus_budget

    hot, plain, ghost = work("hot", hot=True), work("plain"), work("ghost", state="claimed")
    assert focus_budget.select(document([hot, plain]), [plain, hot], "eng", 10, 50, running=[ghost]) == [hot, plain]


@pytest.mark.parametrize(
    "capacity,weight,busy,focused,normal,slots",
    [(10, 50, 2, 1, 0, 0), (10, 50, 4, 4, 10, 1), (10, 90, 8, 0, 10, 2)],
)
def test_allowance_is_bound_by_the_share_the_cap_and_the_free_room(capacity, weight, busy, focused, normal, slots):
    from scripts.swarm import focus_budget

    assert focus_budget.allowance(capacity, weight, busy, focused, normal) == slots
