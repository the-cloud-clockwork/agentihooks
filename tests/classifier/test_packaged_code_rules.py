import pytest

from hooks.classifier import code_rules, definitions, runner
from hooks.classifier.result import Answer
from scripts.swarm import difficulty, grouping, model_pick, priority_sweep, profile_choice, slice_screen, trace_plan
from scripts.swarm_ledger import ledger_duplicates

LEVELS = ["low", "medium", "high", "max"]


def choice(value, confidence):
    return Answer("choice", choice=value, confidence=confidence)


def noul(value):
    return Answer("noul", noul=value)


def score(value, confidence):
    return Answer("score", score=value, confidence=confidence)


def verdicts(module, name, answers, params=None):
    return module.RULE.verdicts(definitions.load(name), {}, params or {}, answers)


def test_asked_builds_the_runner_questions_from_the_params():
    definition = definitions.load("phase-slice")
    params = {"tasks": [{"id": "t1", "title": "one"}, {"id": "t2", "title": "two"}]}
    asked = code_rules.asked(definition, {}, params)
    assert asked == runner.questions_for(definition, params)
    assert sorted(asked) == ["serves_0", "serves_1", "size_0", "size_1"]


@pytest.mark.parametrize(
    ("answer", "expected"),
    [(choice("L", 0.6), "L"), (choice("S", 0.9), "S"), (choice("L", 0.59), "M"), (choice("XL", 0.9), "M")],
)
def test_difficulty_takes_a_confident_option_else_the_fallback(answer, expected):
    assert verdicts(difficulty, "task-difficulty", {"difficulty": answer}) == {"difficulty": expected}


def test_difficulty_without_a_confidence_falls_back():
    assert verdicts(difficulty, "task-difficulty", {"difficulty": choice("L", None)}) == {"difficulty": "M"}


@pytest.mark.parametrize(
    ("answer", "params", "expected"),
    [
        (score(2.2, 0.6), {"levels": LEVELS, "floor": "low"}, "high"),
        (score(0, 0.9), {"levels": LEVELS, "floor": "low"}, "low"),
        (score(0, 0.9), {"levels": LEVELS, "floor": "high"}, "high"),
        (score(3, 0.59), {"levels": LEVELS, "floor": "low"}, "lane default"),
    ],
)
def test_model_pick_raises_to_the_floor_or_keeps_the_lane_default(answer, params, expected):
    assert verdicts(model_pick, "model-pick", {"effort": answer}, params) == {"effort": expected}


def test_model_pick_rejects_with_the_lane_default():
    assert model_pick.RULE.rejections == {"effort": "lane default"}
    assert model_pick.RULE.values == {
        "effort": ("lane default", "high", "low", "max", "medium", "xhigh"),
    }


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (choice("engineer", 0.6), "engineer"),
        (choice("frontend", 0.9), "frontend"),
        (choice("engineer", 0.59), "lane default"),
        (choice("engineer", None), "lane default"),
        (choice("split", 0.9), "unresolved"),
    ],
)
def test_profile_pick_names_the_responsibility_the_lane_default_or_unresolved(answer, expected):
    assert verdicts(profile_choice, "profile-pick", {"responsibility": answer}) == {"profile": expected}


def test_profile_pick_offers_every_responsibility_and_rejects_as_unresolved():
    assert profile_choice.RULE.values == {"profile": ("frontend", "engineer", "qa", "lane default", "unresolved")}
    assert profile_choice.RULE.rejections == {"profile": "unresolved"}


PAIRS = {"pairs": [{"new": 0, "slot": 0}, {"new": 0, "slot": 1}]}


@pytest.mark.parametrize(("first", "second", "expected"), [(0.6, 0.2, False), (0.2, 0.61, True), (0.1, 0.1, False)])
def test_ledger_duplicate_is_any_pair_above_the_floor(first, second, expected):
    answers = {"new_0_existing_0": noul(first), "new_0_existing_1": noul(second)}
    assert verdicts(ledger_duplicates, "ledger-duplicate", answers, PAIRS) == {"duplicate": expected}


@pytest.mark.parametrize(("second", "expected"), [(0.6, True), (0.59, False)])
def test_task_grouping_needs_every_group_confirmed(second, expected):
    answers = {"group_0": noul(0.9), "group_1": noul(second)}
    assert verdicts(grouping, "task-grouping", answers, {"groups": ["a", "b"]}) == {"group": expected}


@pytest.mark.parametrize(("value", "expected"), [(0.5, True), (0.49, False), (None, False)])
def test_priority_resolve_needs_the_probability(value, expected):
    assert verdicts(priority_sweep, "priority-resolve", {"resolves": noul(value)}) == {"resolves": expected}


TASKS = [{"id": "t1", "title": "one"}]


@pytest.mark.parametrize(
    ("size", "serves", "confidence", "expected"),
    [
        (score(2, 0.7), 0.9, 0.7, True),
        (score(2, 0.69), 0.9, 0.7, False),
        (score(1, 0.9), 0.29, 0.7, True),
        (score(1, 0.9), 0.3, 0.7, False),
        (score(2, 0.8), 0.9, 0.9, False),
    ],
)
def test_phase_slice_flags_a_big_or_off_intent_task(size, serves, confidence, expected):
    answers = {"size_0": size, "serves_0": noul(serves)}
    result = verdicts(slice_screen, "phase-slice", answers, {"tasks": TASKS, "confidence": confidence})
    assert result == {"flagged": expected}


def pieces(*rulings, sized=True):
    return {
        "pieces": [{"slot": i, "ruling": ruling} for i, ruling in enumerate(rulings)],
        "sized": [{}] if sized else [],
    }


@pytest.mark.parametrize(
    ("first", "second", "size", "ruling", "expected"),
    [
        (0.3, 0.29, score(1, 0.9), False, "pass"),
        (0.29, 0.29, score(1, 0.9), False, "fail"),
        (0.29, 0.29, score(1, 0.9), True, "pass"),
        (None, 0.29, score(1, 0.9), False, "pass"),
        (0.9, 0.9, score(2, 0.7), False, "fail"),
        (0.9, 0.9, score(2, 0.69), False, "pass"),
    ],
)
def test_trace_plan_fails_a_mostly_cut_or_oversized_plan(first, second, size, ruling, expected):
    answers = {"piece_0": noul(first), "piece_1": noul(second), "size": size}
    assert verdicts(trace_plan, "trace-plan", answers, pieces(ruling, False)) == {"verdict": expected}


def test_trace_plan_passes_a_grown_plan_without_a_size_answer():
    answers = {"piece_2": noul(0.1)}
    params = {"pieces": [{"slot": 2, "ruling": False}], "sized": []}
    assert verdicts(trace_plan, "trace-plan", answers, params) == {"verdict": "pass"}


def test_trace_plan_params_carry_each_piece_ruling():
    fresh = [
        trace_plan.Piece("tests", ("tests/test_a.py",), "proof"),
        trace_plan.Piece("clear", ("mutation-clearances/a.json",), "none"),
    ]
    assert trace_plan._params(fresh, 1, False) == {
        "pieces": [
            {"slot": 1, "number": 2, "what": "tests", "ruling": False},
            {"slot": 2, "number": 3, "what": "clear", "ruling": True},
        ],
        "sized": [],
    }
