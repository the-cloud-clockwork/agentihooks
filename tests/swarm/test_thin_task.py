"""The thin task check and the enhancement plan section grader, each rule red on its own planted fault."""

from typing import Any

import pytest

from scripts.swarm import slice_check
from scripts.swarm.thin_task import section_complete, thin

pytestmark = pytest.mark.unit

FULL = (
    "Add the thin task check module with one message per failed rule, held from engineer claims until a planner "
    "enhances it, and cover every rule and every exemption with a unit test that plants exactly one fault each. "
    "Tests assert thin(task) returns each failed rule. Done when the pull request is merged with CI green."
)
NAMED_SEAMS = (
    "Tests assert `thin_task.thin(task)` returns reasons and `thin_task.section_complete(text)` returns omissions."
)
SECTION = f"""### Thin task check

## What will be built
The check.
## Code areas touched
The module.
## What might break
Nothing.
## Regression tests
The hold tests.
## Resources needed
None.
## Protection plan
Tests first.

## Summary
One check.
## Seams
{NAMED_SEAMS}
## Tests
Unit tests.
## Proof
The CI run.

Done when the pull request is merged.
"""
RECON = (
    "What will be built",
    "Code areas touched",
    "What might break",
    "Regression tests",
    "Resources needed",
    "Protection plan",
)
BODY = ("Summary", "Seams", "Tests", "Proof")


def task(**fields: Any) -> dict[str, Any]:
    return {
        "id": "a",
        "lane": "eng",
        "kind": "code",
        "state": "open",
        "description": FULL,
        "territory": ["scripts/swarm"],
        **fields,
    }


@pytest.mark.feature("swarm-thin-task-check")
def test_a_full_task_is_not_thin() -> None:
    assert thin(task()) == []


@pytest.mark.feature("swarm-thin-task-check")
def test_a_description_under_forty_words_is_named_and_forty_pass() -> None:
    forty = " ".join(["word"] * 31) + " Tests assert thin(task) returns reasons. Done when it merges"
    assert len(forty.split()) == 40
    assert thin(task(description=forty)) == []
    shorter = " ".join(forty.split()[1:])
    assert thin(task(description=shorter)) == ["Task a has a description under 40 words."]


@pytest.mark.feature("swarm-thin-task-check")
def test_a_description_without_a_done_when_sentence_is_named() -> None:
    plain = FULL.replace("Done when", "Finished after")
    assert thin(task(description=plain)) == ["Task a has no done when sentence."]


@pytest.mark.feature("swarm-thin-task-check")
@pytest.mark.parametrize("territory", [[], None])
def test_a_task_without_territory_is_named(territory: list[str] | None) -> None:
    assert thin(task(territory=territory)) == ["Task a names no territory."]


def test_a_task_without_a_territory_field_is_named() -> None:
    bare = {k: v for k, v in task().items() if k != "territory"}
    assert thin(bare) == ["Task a names no territory."]


@pytest.mark.feature("swarm-thin-task-check")
@pytest.mark.parametrize("kind", ["code", "ci"])
def test_code_and_ci_work_without_a_named_seam_is_named(kind: str) -> None:
    unnamed = FULL.replace("Tests assert thin(task) returns each failed rule. ", "")
    assert thin(task(kind=kind, description=unnamed)) == [f"Task a is {kind} work that names no seam."]


def test_work_beyond_code_needs_no_seam() -> None:
    unnamed = FULL.replace("Tests assert thin(task) returns each failed rule. ", "")
    assert thin(task(kind="ops", description=unnamed)) == []


def test_a_task_without_a_kind_reads_as_code() -> None:
    unnamed = FULL.replace("Tests assert thin(task) returns each failed rule. ", "")
    bare = {k: v for k, v in task(description=unnamed).items() if k != "kind"}
    assert thin(bare) == ["Task a is code work that names no seam."]


@pytest.mark.feature("swarm-thin-task-check")
@pytest.mark.parametrize(
    "named",
    [
        "Tests assert hold(task) returns a reason.",
        "Tests observe claim_hold returns a reason.",
        "Tests observe readiness.ready returns false.",
        "Tests assert /api/tasks/one returns status 404.",
        "Tests assert agentihooks swarm exits with status zero.",
        "Tests assert python -m pytest exits with status zero.",
        "Tests assert `README.md` contains the usage instructions.",
    ],
)
def test_each_seam_shape_names_a_seam(named: str) -> None:
    text = FULL.replace("Tests assert thin(task) returns each failed rule.", named)
    assert thin(task(description=text)) == []
    assert section_complete(SECTION.replace(NAMED_SEAMS, named)) == []


@pytest.mark.parametrize(
    "unnamed",
    [
        "It changes the hold only.",
        "It changes a _private(name) only.",
        "It changes e.g. the hold only.",
        "It changes and/or the hold only.",
        "It changes words (like these) only.",
    ],
)
def test_plain_words_name_no_seam(unnamed: str) -> None:
    text = FULL.replace("Tests assert thin(task) returns each failed rule.", unnamed)
    assert thin(task(description=text)) == ["Task a is code work that names no seam."]


@pytest.mark.feature("swarm-thin-task-check")
@pytest.mark.parametrize(
    "seam",
    [
        "Seams are listed.",
        "The seam is listed.",
        "It changes `hold` only.",
        "It changes `some quoted prose` only.",
        "It changes hold(task) only.",
        "It changes `README.md` only.",
        "Tests observe rejected tasks.",
        "Tests assert `some quoted prose` returns reasons.",
        "It changes hold(task). Tests observe rejected tasks.",
        "- It changes hold(task).\n- Tests observe rejected tasks.",
        "Tests assert hold(task).",
        "Tests assert hold(task) returns.",
    ],
)
@pytest.mark.parametrize("kind", ["code", "ci"])
def test_a_thin_seam_needs_a_boundary_and_observation(seam: str, kind: str) -> None:
    text = FULL.replace("Tests assert thin(task) returns each failed rule.", seam)
    assert thin(task(kind=kind, description=text)) == [f"Task a is {kind} work that names no seam."]


@pytest.mark.feature("swarm-thin-task-check")
@pytest.mark.parametrize(
    "seam",
    [
        "The seam is listed.",
        "It changes `some quoted prose` only.",
        "It changes hold(task) only.",
        "Tests observe rejected tasks.",
        "Tests assert `some quoted prose` returns reasons.",
        "It changes hold(task). Tests observe rejected tasks.",
        "- It changes hold(task).\n- Tests observe rejected tasks.",
        "Tests assert hold(task) returns.",
    ],
)
def test_a_section_seam_needs_a_boundary_and_observation(seam: str) -> None:
    assert section_complete(SECTION.replace(NAMED_SEAMS, seam)) == ["The section names no seam."]


def test_every_failed_rule_is_named_at_once() -> None:
    assert thin(task(description="Fix it.", territory=[])) == [
        "Task a has a description under 40 words.",
        "Task a has no done when sentence.",
        "Task a names no territory.",
        "Task a is code work that names no seam.",
    ]


def test_a_task_with_an_empty_description_is_thin() -> None:
    assert thin(task(description="")) == [
        "Task a has a description under 40 words.",
        "Task a has no done when sentence.",
        "Task a is code work that names no seam.",
    ]


@pytest.mark.feature("swarm-thin-task-check")
@pytest.mark.parametrize(
    "fields",
    [
        {"plan_slice": "thin-check"},
        {"kind": "plan", "lane": "plan"},
        {"enhancement": "planned"},
        {"enhancement": "skipped"},
    ],
)
def test_a_sliced_plan_planned_or_skipped_task_is_never_thin(fields: dict[str, Any]) -> None:
    assert thin(task(description="Fix it.", territory=[], **fields)) == []


@pytest.mark.parametrize("mark", ["", "needed"])
def test_an_unchecked_or_needed_task_is_still_graded(mark: str) -> None:
    assert thin(task(description="Fix it.", enhancement=mark, plan_slice="")) == [
        "Task a has a description under 40 words.",
        "Task a has no done when sentence.",
        "Task a is code work that names no seam.",
    ]


@pytest.mark.feature("swarm-thin-task-check")
def test_a_complete_section_passes() -> None:
    assert section_complete(SECTION) == []


@pytest.mark.feature("swarm-thin-task-check")
def test_a_section_without_a_named_seam_is_named() -> None:
    text = SECTION.replace(NAMED_SEAMS, "The two functions.")
    assert section_complete(text) == ["The section names no seam."]


def test_a_seam_named_outside_the_seams_heading_does_not_count() -> None:
    text = SECTION.replace(NAMED_SEAMS, "The two functions.").replace("The module.", NAMED_SEAMS)
    assert section_complete(text) == ["The section names no seam."]


@pytest.mark.feature("swarm-thin-task-check")
@pytest.mark.parametrize("heading", RECON)
def test_each_missing_recon_heading_is_named(heading: str) -> None:
    text = SECTION.replace(f"## {heading}\n", "")
    assert section_complete(text) == [f"The section has no {heading} heading."]


@pytest.mark.feature("swarm-thin-task-check")
@pytest.mark.parametrize("name", [name for name in BODY if name != "Seams"])
def test_each_missing_pull_request_body_section_is_named(name: str) -> None:
    text = SECTION.replace(f"## {name}\n", "")
    assert section_complete(text) == [f"The section has no {name} pull request body section."]


def test_a_missing_seams_heading_leaves_no_named_seam_either() -> None:
    text = SECTION.replace("## Seams\n", "")
    assert section_complete(text) == [
        "The section names no seam.",
        "The section has no Seams pull request body section.",
    ]


@pytest.mark.feature("swarm-thin-task-check")
def test_a_section_without_a_done_when_sentence_is_named() -> None:
    text = SECTION.replace("Done when the pull request is merged.", "Finished.")
    assert section_complete(text) == ["The section has no done when sentence."]


def test_a_heading_named_inside_a_line_does_not_count() -> None:
    text = SECTION.replace("## Protection plan\n", "The Protection plan is tests first.\n")
    assert section_complete(text) == ["The section has no Protection plan heading."]


def test_a_heading_with_nothing_under_it_counts_as_missing() -> None:
    text = SECTION.replace("## Protection plan\nTests first.\n", "## Protection plan\n")
    assert section_complete(text) == ["The section has no Protection plan heading."]


def test_headings_match_at_any_level_and_any_case() -> None:
    text = SECTION.replace("## Summary", "#### summary").replace("## What might break", "# WHAT MIGHT BREAK")
    assert section_complete(text) == []


def test_an_empty_section_names_every_part() -> None:
    assert section_complete("") == [
        "The section names no seam.",
        *(f"The section has no {heading} heading." for heading in RECON),
        *(f"The section has no {name} pull request body section." for name in BODY),
        "The section has no done when sentence.",
    ]


@pytest.mark.parametrize(
    ("words", "floor", "short"), [(19, 20, True), (20, 20, False), (39, 40, True), (40, 40, False)]
)
def test_the_word_rule_counts_words_below_its_floor(words: int, floor: int, short: bool) -> None:
    assert slice_check.short(" ".join(["word"] * words), floor) is short


def test_the_word_rule_defaults_to_the_slice_floor() -> None:
    assert slice_check.short(" ".join(["word"] * 19)) is True
    assert slice_check.short(" ".join(["word"] * 20)) is False


@pytest.mark.parametrize(
    ("text", "found"),
    [("Done when merged.", True), ("It is DONE WHEN green.", True), ("Abandoned whenever.", False), ("", False)],
)
def test_the_done_when_rule_finds_the_sentence(text: str, found: bool) -> None:
    assert slice_check.done_when(text) is found
