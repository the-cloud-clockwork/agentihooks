import pytest


def work(name, *, lane="eng", state="open", hot=False, rank="normal"):
    return {"id": name, "lane": lane, "state": state, "kind": "hot" if hot else "code", "rank": rank}


def document(tasks):
    return {"tasks": tasks, "phases": [], "plans": [], "freezes": [{"verb": "focus", "target": "kind:hot"}]}


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
    hot = [work(f"hot{i}", hot=True) for i in range(3)]
    plain = [work("normal")]
    assert focus_budget.select(document(held + hot + plain), plain + hot, "eng", 10, 50) == hot[:1] + plain


def test_focus_is_held_at_the_cap_even_if_normal_work_is_absent():
    from scripts.swarm import focus_budget

    held = [work(f"held{i}", hot=True, state="claimed") for i in range(5)]
    hot = work("waiting", hot=True, rank="urgent")
    assert focus_budget.select(document(held + [hot]), [hot], "eng", 10, 50) == []


def test_focus_cap_is_independent_per_lane():
    from scripts.swarm import focus_budget

    held = [work(f"held{i}", hot=True, state="claimed") for i in range(5)]
    hot = work("ci", lane="ci", hot=True)
    assert focus_budget.select(document(held + [hot]), [hot], "ci", 2, 50) == [hot]


@pytest.mark.parametrize("capacity,weight,count", [(10, 0, 0), (10, 100, 10), (3, 50, 1), (1, 50, 0)])
def test_percentage_boundaries_use_integer_floor(capacity, weight, count):
    from scripts.swarm import focus_budget

    hot = [work(f"hot{i}", hot=True) for i in range(10)]
    assert focus_budget.select(document(hot), hot, "eng", capacity, weight) == hot[:count]


def test_unfocused_tasks_keep_their_existing_rank_order():
    from scripts.swarm import focus_budget

    plain = [work("urgent", rank="urgent"), work("normal")]
    assert focus_budget.select(document(plain), plain, "eng", 10, 50) == plain
