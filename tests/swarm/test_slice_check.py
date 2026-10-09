import pytest

from scripts.swarm import slice_check
from scripts.swarm.slice_check import Limits

DONE_WHEN = (
    "Add the parser for the new phase field and cover each case with a unit test. Done when the tests pass in CI."
)
PHASE = {"id": "p1", "title": "Build"}


def task(tid, **fields):
    return {
        "id": tid,
        "title": f"Task {tid}",
        "phase": "p1",
        "lane": "eng",
        "kind": "code",
        "state": "open",
        "description": DONE_WHEN,
        "territory": ["scripts/swarm"],
        "depends_on": [],
        **fields,
    }


def doc(*tasks, slice_ids=None, phases=None):
    ids = ",".join(t["id"] for t in tasks) if slice_ids is None else slice_ids
    plan = {
        "id": "plan-p1",
        "phase": "p1",
        "lane": "plan",
        "kind": "plan",
        "state": "done",
        "proof": {"slice": ids},
    }
    return {"phases": phases or [PHASE], "tasks": [plan, *tasks]}


def check(document, limits=Limits()):
    return slice_check.check(PHASE, document, limits)


def test_a_clean_slice_has_no_problems():
    assert check(doc(task("a"), task("b", depends_on=["a"]))) == []


def test_an_empty_slice_is_named():
    assert check(doc(slice_ids="")) == ["The slice holds no task."]


def test_a_slice_over_the_task_limit_is_named():
    tasks = [task(f"t{i}") for i in range(3)]
    assert check(doc(*tasks), Limits(max_tasks=2)) == ["The slice holds 3 tasks, more than 2."]
    assert check(doc(*tasks), Limits(max_tasks=3)) == []


def test_a_task_outside_the_phase_is_named():
    other = task("x", phase="p2")
    assert check(doc(task("a"), other)) == ["Task x is not in this phase."]


def test_an_unknown_slice_id_is_named_as_outside_the_phase():
    assert check(doc(task("a"), slice_ids="a, ghost")) == ["Task ghost is not in this phase."]


def test_a_short_description_is_named():
    short = "Fix it. Done when green."
    assert check(doc(task("a", description=short))) == ["Task a has a description under 20 words."]


def test_twenty_words_with_a_done_condition_pass():
    words = " ".join(["word"] * 17) + " done when merged"
    assert len(words.split()) == 20
    assert check(doc(task("a", description=words))) == []
    assert check(doc(task("a", description=words.split(" ", 1)[1]))) == ["Task a has a description under 20 words."]


def test_a_description_without_a_done_condition_is_named():
    plain = " ".join(["word"] * 25)
    assert check(doc(task("a", description=plain))) == ["Task a has no done when sentence."]


def test_work_beyond_code_needs_must_check_and_judge():
    ops = task("a", kind="ops", territory=[], contract={"must": "m", "check": "c"})
    assert check(doc(ops)) == ["Task a is ops work without must, check and judge in its contract."]
    full = task("a", kind="ops", territory=[], contract={"must": "m", "check": "c", "judge": "j"})
    assert check(doc(full)) == []


@pytest.mark.parametrize("kind", ["code", "ci"])
def test_code_and_ci_tasks_name_a_territory(kind):
    lane = "ci" if kind == "ci" else "eng"
    assert check(doc(task("a", kind=kind, lane=lane, territory=[]))) == ["Task a names no territory."]


def test_too_many_territory_areas_are_named():
    areas = [f"area{i}" for i in range(3)]
    assert check(doc(task("a", territory=areas)), Limits(max_areas=2)) == ["Task a names 3 areas, more than 2."]
    assert check(doc(task("a", territory=areas)), Limits(max_areas=3)) == []


def test_a_dependency_on_an_unfinished_other_phase_is_named():
    phases = [PHASE, {"id": "p0", "title": "Before", "done": False}]
    elsewhere = {"id": "z", "phase": "p0", "state": "open"}
    document = doc(task("a", depends_on=["z"]), phases=phases)
    document["tasks"].append(elsewhere)
    assert check(document) == ["Task a depends on z in a phase that is not done."]
    phases[1]["done"] = True
    assert check(document) == []


def test_a_dependency_cycle_inside_the_slice_is_named():
    cycle = doc(task("a", depends_on=["b"]), task("b", depends_on=["a"]))
    assert check(cycle) == ["The slice has a dependency cycle."]


def test_a_slice_task_in_the_plan_lane_is_named():
    assert check(doc(task("a", lane="plan"))) == ["Task a is in the plan lane."]


def test_every_problem_is_reported_at_once():
    bad = task("a", description="short", territory=[], lane="plan")
    assert check(doc(bad)) == [
        "Task a has a description under 20 words.",
        "Task a names no territory.",
        "Task a is in the plan lane.",
    ]


def test_limits_read_the_environment_with_defaults():
    assert Limits.from_env({}) == Limits(12, 6)
    env = {"AGENTIHOOKS_PLAN_MAX_TASKS": "4", "AGENTIHOOKS_PLAN_MAX_AREAS": "2"}
    assert Limits.from_env(env) == Limits(4, 2)


def test_the_plan_task_of_this_phase_is_read_even_after_another_phases_plan():
    document = doc(task("a"))
    other = {"id": "plan-p2", "phase": "p2", "lane": "plan", "kind": "plan", "state": "done", "proof": {"slice": ""}}
    document["tasks"].insert(0, other)
    assert check(document) == []


def test_an_unknown_slice_id_does_not_stop_the_check_of_later_tasks():
    assert check(doc(task("a", description="short"), slice_ids="ghost,a")) == [
        "Task ghost is not in this phase.",
        "Task a has a description under 20 words.",
    ]


def test_a_task_without_a_description_is_named():
    bare = task("a")
    del bare["description"]
    assert check(doc(bare)) == ["Task a has a description under 20 words."]


def test_a_dependency_the_ledger_does_not_know_is_not_a_slice_problem():
    assert check(doc(task("a", depends_on=["ghost"]))) == []


def test_every_phase_task_linking_the_plan_without_plan_lines_is_named():
    url = "http://127.0.0.1:8765/artifacts/demo/plan.md"
    document = doc(task("a", plan_url=url), task("b", plan_url=url, plan_slice="b", plan_lines="3-4"))
    document["tasks"][0]["plan_url"] = url
    document["tasks"] += [task("c", plan_url=url), task("d", phase="p2", plan_url=url), task("e")]
    assert check(document) == [
        "Task a links the plan but has no plan lines.",
        "Task c links the plan but has no plan lines.",
    ]
