import pytest

from scripts.gates import intent
from tests.gates.test_intent import PR, chunk_classifier, planned  # noqa: F401

pytestmark = pytest.mark.unit

FIRST = "https://github.com/o/r/pull/9"
SECOND = "https://github.com/o/r/pull/10"


def split(doc, pr_url=FIRST, pulls=(), lines="15-17"):
    task = {**doc["tasks"][0], "plan_lines": lines, "split": ["15-16", "17-17"], "split_pulls": list(pulls)}
    return {**doc, "tasks": [{**task, "pr_url": pr_url}]}


def test_the_first_pull_request_of_a_split_task_is_judged_against_its_own_half(planned):  # noqa: F811
    doc = split(planned)
    state = intent.state_of(doc, doc["tasks"][0], PR)
    assert (state["plan_lines"], state["plan_chunk"]) == ("15-16", "line 15\nline 16\n")
    assert state["task_text"].endswith(
        "This pull request is half 1 of 2 of a split task: it carries only plan lines 15-16; "
        "half 2 carries plan lines 17-17 in its own pull request."
    )
    assert intent.judge(state, decide=chunk_classifier()) == (
        "pass",
        "the phase can use it as delivered at probability 0.90, judged against plan lines 15-16",
    )


def test_a_bound_pull_request_is_judged_against_the_half_it_was_bound_to(planned):  # noqa: F811
    doc = split(planned, pr_url=SECOND, pulls=[FIRST, SECOND])
    state = intent.state_of(doc, doc["tasks"][0], PR)
    assert (state["plan_lines"], state["plan_chunk"]) == ("17-17", "line 17\n")
    assert state["task_text"].endswith(
        "This pull request is half 2 of 2 of a split task: it carries only plan lines 17-17; "
        "half 1 carries plan lines 15-16 in its own pull request."
    )


def test_the_next_unbound_pull_request_takes_the_next_half(planned):  # noqa: F811
    doc = split(planned, pr_url=SECOND, pulls=[FIRST])
    assert intent.state_of(doc, doc["tasks"][0], PR)["plan_lines"] == "17-17"


def test_the_task_text_names_every_other_half():
    task = {"description": "Three halves.", "pr_url": SECOND, "split": ["1-2", "3-4", "5-5"], "split_pulls": [FIRST]}
    assert intent._split_note(task) == (
        "\n\nThis pull request is half 2 of 3 of a split task: it carries only plan lines 3-4; "
        "half 1 carries plan lines 1-2; half 3 carries plan lines 5-5 in its own pull request."
    )


def test_a_pull_request_past_the_last_half_is_judged_on_the_whole_task(planned):  # noqa: F811
    third = "https://github.com/o/r/pull/11"
    doc = split(planned, pr_url=third, pulls=[FIRST, SECOND])
    task = doc["tasks"][0]
    state = intent.state_of(doc, task, PR)
    assert (state["plan_lines"], state["task_text"]) == ("15-17", task["description"])


@pytest.mark.parametrize("pulls", [[], [FIRST]])
def test_the_half_is_part_of_the_fingerprint(planned, pulls):  # noqa: F811
    whole = {**planned["tasks"][0], "pr_url": SECOND}
    halved = split(planned, pr_url=SECOND, pulls=pulls)["tasks"][0]
    assert intent._fingerprint(planned, whole) != intent._fingerprint(planned, halved)


def test_split_half_names_the_index_and_lines():
    task = {"pr_url": SECOND, "split": ["1-2", "3-4", "5-5"], "split_pulls": [FIRST]}
    assert intent._split_half(task) == (1, "3-4")
    assert intent._split_half({**task, "pr_url": FIRST}) == (0, "1-2")
    assert intent._split_half({"pr_url": FIRST}) is None
    assert intent._split_half({**task, "split_pulls": [FIRST, "a", "b"], "pr_url": "c"}) is None
