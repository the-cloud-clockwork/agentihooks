import pytest


def work(name, *, lane="eng", state="open", hot=False, rank="normal"):
    return {"id": name, "lane": lane, "state": state, "kind": "hot" if hot else "code", "rank": rank}


def document(tasks):
    return {"tasks": tasks, "phases": [], "plans": [], "freezes": [{"verb": "focus", "target": "kind:hot"}]}


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


def test_ranks_take_the_first_focus_covering_a_task_by_path_or_selector():
    from scripts.swarm import focus_budget

    inside, hot, both = (
        {**work("inside"), "phase": "p1"},
        work("hot", hot=True),
        {**work("both", hot=True), "phase": "p1"},
    )
    colon, odd = work("a:b"), {**work("odd"), "kind": "x:y"}
    found = {
        "tasks": [work("outside"), inside, hot, both, work("ci", lane="ci"), colon, odd],
        "phases": [{"id": "p1"}],
        "plans": [],
        "freezes": [
            {"verb": "freeze", "target": "phases/p1"},
            {"verb": "focus", "target": "kind:hot"},
            {"verb": "focus", "target": "phases/p1"},
            {"verb": "focus", "target": "kind:hot"},
            {"verb": "focus", "target": "lane:ci"},
            {"verb": "focus", "target": "lane:"},
            {"verb": "focus", "target": "tasks/a:b"},
            {"verb": "focus", "target": "kind:x:y"},
        ],
    }
    ranked = {"outside": 7, "inside": 1, "hot": 0, "both": 0, "ci": 3, "a:b": 5, "odd": 6}
    assert focus_budget.ranks(found) == ranked


def test_ranks_match_path_focuses_without_testing_each_one_against_every_task(monkeypatch):
    from scripts.swarm import focus_budget, freeze

    calls = []
    covers = freeze.covers
    monkeypatch.setattr(freeze, "covers", lambda *args: calls.append(args[0]) or covers(*args))
    found = {
        "tasks": [work(f"t{n}") for n in range(20)],
        "phases": [],
        "plans": [],
        "freezes": [{"verb": "focus", "target": f"tasks/t{n}"} for n in range(20)]
        + [{"verb": "focus", "target": "kind:hot"}],
    }
    assert focus_budget.ranks(found) == {f"t{n}": n for n in range(20)}
    assert calls == []


def test_the_focus_sort_orders_the_queue_by_focus_rank_and_keeps_ties_in_queue_order() -> None:
    from scripts.swarm import focus_budget

    queue = [("eng", work("a")), ("ci", work("b")), ("eng", work("c")), ("eng", work("d"))]
    assert focus_budget.ordered(queue, {"a": 1, "b": 0, "c": 1, "d": 0}) == [queue[1], queue[3], queue[0], queue[2]]


@pytest.mark.feature("swarm-focus")
def test_the_focus_sort_puts_focused_tasks_ahead_of_an_unfocused_task_in_a_better_band() -> None:
    from scripts.swarm import focus_budget

    queue = [("eng", work("free", rank="urgent")), ("eng", work("held"))]
    band = {"urgent": 0, "normal": 1}
    ordered = focus_budget.ordered(queue, {"free": 2, "held": 0}, lambda task: band[task["rank"]], 2)
    assert ordered == [queue[1], queue[0]]
