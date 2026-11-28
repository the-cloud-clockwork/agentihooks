"""The thin task check returns the same problems and seam verdicts while it scans each part's observations once and
grades an unchanged task once."""

import re
from typing import Any

import pytest

from scripts.swarm import thin_task

pytestmark = pytest.mark.unit

WORDS = " ".join(["word"] * 45)
LONG = (
    "Rework scripts.swarm.capacity.apply and scripts.swarm.quota_notice.refresh and scripts.tick.steps.place "
    "and scripts.swarm.quota_handoff.warn and scripts.routing.reservation.master_agents and "
    "scripts.swarm.store.agents and scripts.swarm.capacity.read so the step reads each once, "
    + WORDS
    + " and the step returns the same actions. Done when the pull request is merged."
)
MANY = (
    "Touch scripts.a.b, scripts.c.d, scripts.e.f, scripts.g.h, scripts.i.j, run_once(, python -m scripts.k and "
    "agentihooks swarm, the function `ready_work` and the file `capacity.py` without naming any outcome at all "
    + WORDS
    + ". Done when the pull request is merged."
)
LATE = (
    "It returns nothing useful today for anyone reading the rows "
    + WORDS
    + " until the reader reaches scripts.swarm.thin_task.thin at the very end of this one long part. Done when merged."
)
SPLIT = (
    "Change scripts.swarm.thin_task.thin so grading is cheaper for every pass. "
    + WORDS
    + ". It returns the same list. Done when the pull request is merged."
)
LISTED = (
    "Plan for the thin step " + WORDS + "\n1. scripts.swarm.thin_task.thin\n2. it returns the same problems\n"
    "# Heading scripts.tick.steps.thin returns lines\nDone when the pull request is merged."
)
SEAMED = SPLIT.replace("cheaper for every pass.", "cheaper and returns the same list.")
NO_SEAM = "Task {} is code work that names no seam."


class Counting:
    """A compiled pattern stand in that counts its ``finditer`` scans."""

    def __init__(self, pattern: re.Pattern[str]) -> None:
        self.pattern = pattern
        self.scans = 0

    def finditer(self, text: str) -> Any:
        self.scans += 1
        return self.pattern.finditer(text)


def task(tid: str, description: str, **fields: Any) -> dict[str, Any]:
    return {
        "id": tid,
        "lane": "eng",
        "kind": "code",
        "state": "open",
        "description": description,
        "territory": ["scripts/swarm"],
        **fields,
    }


@pytest.mark.feature("tick-quota-thin-cpu")
@pytest.mark.parametrize(
    ("tid", "description", "seam", "problems"),
    [
        ("long", LONG, True, []),
        ("many", MANY, False, [NO_SEAM.format("many")]),
        ("late", LATE, True, []),
        ("split", SPLIT, False, [NO_SEAM.format("split")]),
        ("listed", LISTED, True, []),
    ],
)
def test_thin_and_named_seam_pin_each_corpus_task(tid: str, description: str, seam: bool, problems: list[str]) -> None:
    assert thin_task._named_seam(description) is seam  # pyright: ignore[reportPrivateUsage]
    assert thin_task.thin(task(tid, description)) == problems


@pytest.mark.feature("tick-quota-thin-cpu")
@pytest.mark.parametrize(
    ("text", "seam"),
    [("a(returns x", False), ("a( returns x", True), ("returns (ab.cd", False), ("returns ( ab.cd", True)],
)
def test_a_boundary_touching_its_observation_is_no_seam(text: str, seam: bool) -> None:
    assert thin_task._named_seam(text) is seam  # pyright: ignore[reportPrivateUsage]


@pytest.mark.feature("tick-quota-thin-cpu")
def test_named_seam_scans_the_observations_of_each_part_once(monkeypatch: pytest.MonkeyPatch) -> None:
    counting = Counting(thin_task.OBSERVED)
    monkeypatch.setattr(thin_task, "OBSERVED", counting)
    assert thin_task._named_seam(MANY) is False  # pyright: ignore[reportPrivateUsage]
    assert counting.scans == 2


@pytest.mark.feature("tick-quota-thin-cpu")
def test_a_second_grading_of_an_unchanged_task_scans_no_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    named = thin_task._named_seam  # pyright: ignore[reportPrivateUsage]

    def counted(text: str) -> bool:
        seen.append(text)
        return named(text)

    monkeypatch.setattr(thin_task, "_named_seam", counted)
    first = thin_task.thin(task("many", MANY))
    second = thin_task.thin(task("many", MANY))
    assert first == second == [NO_SEAM.format("many")]
    assert seen == [MANY]


@pytest.mark.feature("tick-quota-thin-cpu")
@pytest.mark.parametrize(
    ("fields", "problems"),
    [
        ({}, [NO_SEAM.format("edit")]),
        ({"description": SEAMED}, []),
        ({"territory": []}, ["Task edit names no territory.", NO_SEAM.format("edit")]),
        ({"kind": "ops"}, []),
        ({"id": "other"}, [NO_SEAM.format("other")]),
        ({"plan_slice": "s"}, []),
        ({"enhancement": "planned"}, []),
        (
            {"description": "Fix the claim."},
            [
                "Task edit has a description under 40 words.",
                "Task edit has no done when sentence.",
                NO_SEAM.format("edit"),
            ],
        ),
    ],
)
def test_an_edited_task_is_graded_again_after_its_first_grading(fields: dict[str, Any], problems: list[str]) -> None:
    assert thin_task.thin(task("edit", SPLIT)) == [NO_SEAM.format("edit")]
    assert thin_task.thin({**task("edit", SPLIT), **fields}) == problems


@pytest.mark.feature("tick-quota-thin-cpu")
def test_a_returned_problem_list_is_the_callers_own() -> None:
    first = thin_task.thin(task("many", MANY))
    first.append("changed by the caller")
    assert thin_task.thin(task("many", MANY)) == [NO_SEAM.format("many")]


@pytest.mark.feature("tick-quota-thin-cpu")
def test_the_grading_memo_forgets_only_the_oldest_task_past_four_thousand_ninety_six(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index in range(4097):
        thin_task.thin(task(f"t{index}", MANY))
    seen: list[str] = []
    named = thin_task._named_seam  # pyright: ignore[reportPrivateUsage]

    def counted(text: str) -> bool:
        seen.append(text)
        return named(text)

    monkeypatch.setattr(thin_task, "_named_seam", counted)
    assert thin_task.thin(task("t1", MANY)) == [NO_SEAM.format("t1")]
    assert seen == []
    assert thin_task.thin(task("t0", MANY)) == [NO_SEAM.format("t0")]
    assert seen == [MANY]
