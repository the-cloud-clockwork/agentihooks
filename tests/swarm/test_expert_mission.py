"""The expert mission document: its template, its structural check, its deliverables and its address."""

import hashlib

import pytest

from scripts.handoff import check
from scripts.swarm import expert_mission as mission

pytestmark = pytest.mark.unit

GOOD = """# Mission: Keep an ordered batch built

## The operator's words
"Why is it so difficult to keep a batch being built", 21 November, master pane.

## The problem
A batch he orders is filed, then sits unclaimed while agents work on other things.

## Evidence
1. The focus list was empty while 1208 tasks were open (observed).
2. Masters clear focus after one claim (unproven).

## What to deliver
1. Prove each cause against the live ledger.
2. A plan file with one slice per pull request.
3. The implementation, merged.

## How to report
Tell the master by inbox when each pull request merges.
"""
ORDER = (
    "the headings are out of order; keep The operator's words, The problem, Evidence, What to deliver, How to report"
)
NUMBERING = "## What to deliver numbers its deliverables from 1 with no gap, one per line"


def _swap(old: str, new: str) -> str:
    assert old in GOOD
    return GOOD.replace(old, new)


@pytest.mark.feature("expert-mission-and-lineage")
def test_the_template_passes_its_own_check() -> None:
    assert mission.problems(mission.TEMPLATE) == []
    assert mission.title(mission.TEMPLATE) == "<one line naming the outcome>"
    assert len(mission.deliverables(mission.TEMPLATE)) == 2
    assert mission.TEMPLATE.startswith("# Mission: ") and mission.TEMPLATE.endswith("\n")
    for heading in ("The operator's words", "The problem", "Evidence", "What to deliver", "How to report"):
        assert f"\n## {heading}\n" in mission.TEMPLATE


@pytest.mark.feature("expert-mission-and-lineage")
def test_a_conforming_mission_has_no_problems_and_gives_its_title_and_deliverables() -> None:
    assert mission.problems(GOOD) == []
    assert mission.title(GOOD) == "Keep an ordered batch built"
    assert mission.deliverables(GOOD) == [
        "Prove each cause against the live ledger.",
        "A plan file with one slice per pull request.",
        "The implementation, merged.",
    ]


def test_blank_lines_before_the_title_and_spaces_around_it_are_left_out() -> None:
    text = "\n\n  # Mission:   Keep it built  \n" + GOOD.partition("\n")[2]
    assert mission.problems(text) == []
    assert mission.title(text) == "Keep it built"


@pytest.mark.parametrize("first", ["# Mission:", "# Mission: ", "# Mission", "# Handoff v2", "Mission: Keep it built"])
def test_a_first_line_that_is_no_mission_title_is_refused(first: str) -> None:
    text = first + "\n" + GOOD.partition("\n")[2]
    assert mission.title(text) == ""
    assert mission.problems(text) == ["the first line must be # Mission: followed by the mission's title"]


@pytest.mark.parametrize("heading", mission.HEADINGS)
def test_a_missing_section_is_refused_by_name(heading: str) -> None:
    found = mission.problems(_swap(f"## {heading}\n", ""))
    assert f"## {heading} must appear once and appears 0 times" in found


@pytest.mark.parametrize("heading", mission.HEADINGS)
def test_a_section_written_twice_is_refused_by_name_and_is_not_called_out_of_order(heading: str) -> None:
    found = mission.problems(GOOD + f"\n## {heading}\n1. Again.\n")
    assert found == [f"## {heading} must appear once and appears 2 times"]


def test_a_section_first_written_out_of_place_and_then_again_is_refused_for_both() -> None:
    text = _swap("## The problem\n", "## How to report\nEarly.\n\n## The problem\n")
    assert mission.problems(text) == ["## How to report must appear once and appears 2 times", ORDER]


@pytest.mark.parametrize("heading", mission.HEADINGS)
def test_an_empty_section_is_refused_by_name(heading: str) -> None:
    body = GOOD.split(f"## {heading}\n")[1].split("\n## ")[0]
    assert mission.problems(_swap(f"## {heading}\n{body}", f"## {heading}\n\n")) == [f"## {heading} is empty"]


def test_sections_out_of_order_are_refused() -> None:
    problem, evidence = (GOOD.split(f"## {name}\n")[1].split("\n## ")[0] for name in ("The problem", "Evidence"))
    text = _swap(
        f"## The problem\n{problem}\n## Evidence\n{evidence}", f"## Evidence\n{evidence}\n## The problem\n{problem}"
    )
    assert mission.problems(text) == [ORDER]


def test_another_section_and_a_heading_inside_a_fenced_block_are_allowed() -> None:
    extra = "## Constraints\nNo live patch.\n\n```\n## The problem\nquoted, not a section\n```\n\n## What to deliver"
    text = _swap("## What to deliver", extra)
    assert mission.problems(text) == []
    assert check.headings(text) == [
        "The operator's words",
        "The problem",
        "Evidence",
        "Constraints",
        "What to deliver",
        "How to report",
    ]


@pytest.mark.parametrize(
    "listed",
    [
        "1. First.\n3. Third.",
        "2. Second.\n3. Third.",
        "0. Zeroth.\n1. First.",
        "1. First.\n1. First again.",
        "- First.\n- Second.",
        "First, then second.",
        "Do these:\n  1. Indented, so no deliverable.",
    ],
)
def test_deliverables_that_do_not_count_from_one_are_refused(listed: str) -> None:
    text = (
        GOOD.split("## What to deliver\n")[0] + f"## What to deliver\n{listed}\n\n## How to report\nTell the master.\n"
    )
    assert mission.deliverables(text) == []
    assert mission.problems(text) == [NUMBERING]


def test_deliverables_take_either_number_style_and_leave_out_the_lines_between() -> None:
    listed = "1) First, with its check.\n   - a note under the first\n\n2.   Second.  \nA line of prose.\n3. Third."
    text = (
        GOOD.split("## What to deliver\n")[0] + f"## What to deliver\n{listed}\n\n## How to report\nTell the master.\n"
    )
    assert mission.problems(text) == []
    assert mission.deliverables(text) == ["First, with its check.", "Second.", "Third."]


def test_numbered_lines_of_another_section_are_no_deliverables() -> None:
    assert len(mission.deliverables(GOOD)) == 3
    assert mission.deliverables("# Mission: x\n\n## Evidence\n1. Observed.\n") == []


@pytest.mark.parametrize("secret", ["ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8", "AKIA" + "IOSFODNN7EXAMPLE"])
def test_a_credential_value_is_refused_whatever_secret_mode_the_session_runs(
    monkeypatch: pytest.MonkeyPatch, secret: str
) -> None:
    monkeypatch.setattr("hooks.config.SECRETS_MODE", "off")
    [found] = mission.problems(_swap("sits unclaimed", f"sits unclaimed with {secret}"))
    assert found.startswith("credential value found (") and found.endswith("); refer to it by its variable name")
    assert check.leaks(f"token {secret}") == [found]
    assert check.leaks("no secret here") == []


def test_every_problem_is_reported_at_once() -> None:
    text = "# Notes\n\n## What to deliver\n- fix it\n\n## The problem\nIt breaks.\n\n## The problem\nAgain.\n"
    assert mission.problems(text) == [
        "the first line must be # Mission: followed by the mission's title",
        "## The operator's words must appear once and appears 0 times",
        "## The problem must appear once and appears 2 times",
        "## Evidence must appear once and appears 0 times",
        "## How to report must appear once and appears 0 times",
        ORDER,
        NUMBERING,
    ]


@pytest.mark.feature("expert-mission-and-lineage")
def test_the_same_text_has_the_same_address_and_another_text_or_swarm_another() -> None:
    digest = hashlib.sha256(GOOD.encode("utf-8")).hexdigest()
    assert mission.digest(GOOD) == digest
    assert mission.digest("é") == hashlib.sha256("é".encode()).hexdigest()
    assert mission.address("sw", GOOD) == f"mission:sw/{digest}"
    assert mission.address("sw", GOOD) == mission.address("sw", str(GOOD))
    assert mission.address("other", GOOD) == f"mission:other/{digest}"
    assert mission.address("sw", GOOD + " ") != mission.address("sw", GOOD)
    assert check.ADDRESS.fullmatch(mission.address("rig-grade.swarm", GOOD))
    assert mission.line("sw", GOOD) == f"mission:sw/{digest} Keep an ordered batch built"


@pytest.mark.parametrize(
    "address",
    ["mission:sw/abc", "mission:sw/" + "g" * 64, "mission:/" + "a" * 64, "mission:sw/" + "a" * 65, "mission:sw"],
)
def test_a_malformed_mission_address_is_no_address(address: str) -> None:
    assert check.ADDRESS.fullmatch(address) is None
    assert "mission:" in check.ADDRESS_FORMS and "seat:" in check.ADDRESS_FORMS
