"""The expert handoff contract: its ten headings, the mission line, the deliverable accounting and the evidence."""

import pytest

from scripts.handoff import expert
from scripts.swarm import expert_mission
from tests.swarm.test_expert_mission import GOOD

pytestmark = pytest.mark.unit

LINE = expert_mission.line("sw", GOOD)
ADDRESS = expert_mission.address("sw", GOOD)
HEADINGS = (
    "Mission, Done, Pending, Assumptions, Tests, Validation, Stopped at, Decisions and promises, Next, Read first"
)
OPENS = f"## Mission must open with the mission line: {LINE}"
D1 = "- D1 Each cause proven against the live ledger, run 4471 green\n"
D2 = "- D2 The plan file holds two of its slices\n"
D3 = "- D3 The implementation, nothing merged\n"
TESTED = "- Seam one went red then green, run 4472\n"
VALIDATED = "- The deployed tick claimed the batch, `agentihooks swarm sw status` printed 3 claims\n"
VALID = f"""# Expert handoff
## Mission
{LINE}
## Done
{D1}## Pending
{D2}{D3}## Assumptions
- The focus list is read once per tick
## Tests
{TESTED}## Validation
{VALIDATED}## Stopped at
Slice two of the plan, its test red for the expected reason.
## Decisions and promises
None
## Next
1. Finish slice two; done when its test passes.
## Read first
- {ADDRESS} the mission and its deliverables
- recap:expert-1@sw what the last expert did
<!-- handoff complete -->
"""
FAULTY = f"""# Handoff v2
## Mission
{LINE}
## Intent
Keep the batch built.
## Pending
- D2 The plan file
- D3 The implementation
## Done
- D1 Each cause proven
## Tests
## Tests
- Seam one green
## Validation
- Checked by hand, hypothesis until the run lands
## Stopped at
Slice two in hooks/context/recycle.py.
## Decisions and promises
None
## Next
1. Finish slice two.
## Read first
the mission
"""


def _swap(old: str, new: str, text: str = VALID) -> str:
    assert old in text
    return text.replace(old, new, 1)


def _problems(text: str, mission: str = GOOD) -> list[str]:
    return expert.problems(text, lambda address: True, "sw", mission)


def _unnamed(number: int) -> str:
    return f"deliverable D{number} is named by no Done or Pending bullet; start one with D{number}"


def _stray(number: str) -> str:
    return (
        f"D{number} names no deliverable, the mission has 3; number the bullet after the deliverable it accounts "
        "for or drop the number"
    )


def _unevidenced(heading: str) -> str:
    return f"{heading} bullet 1 carries no evidence; add it or label the bullet hypothesis"


@pytest.mark.feature("expert-handoff-contract")
def test_a_conforming_expert_handoff_has_no_problems() -> None:
    assert _problems(VALID) == []


@pytest.mark.feature("expert-handoff-contract")
@pytest.mark.parametrize(
    "opened",
    [
        ADDRESS,
        expert_mission.line("other", GOOD),
        f"{LINE} and more",
        f"The mission of this lineage\n{LINE}",
        "None",
    ],
)
def test_the_mission_section_opens_with_the_mission_line_of_the_lineage(opened: str) -> None:
    assert _problems(_swap(f"## Mission\n{LINE}\n", f"## Mission\n{opened}\n")) == [OPENS]


def test_the_mission_line_may_be_followed_by_the_experts_own_words() -> None:
    assert _problems(_swap(f"{LINE}\n", f"{LINE}\nThe third expert of this lineage.\n")) == []


@pytest.mark.feature("expert-handoff-contract")
@pytest.mark.parametrize(
    "old,new,found",
    [
        (D3, "", [_unnamed(3)]),
        (D3, "- Nothing merged for D3\n", [_unnamed(3)]),
        (D3, "- D3rd deliverable, nothing merged\n", [_unnamed(3)]),
        (D1, "None\n", [_unnamed(1)]),
        (
            D1,
            D1 + "- D2 The plan file merged, run 4473\n",
            ["deliverable D2 is named by 2 bullets; name it once, under Done or under Pending"],
        ),
        (
            D3,
            D3 + D3 + D3,
            ["deliverable D3 is named by 3 bullets; name it once, under Done or under Pending"],
        ),
        (D3, D3 + "- D4 A fourth thing\n", [_stray("4")]),
        (D3, D3 + "- D7 A seventh thing\n- D0 Nothing\n", [_stray("0"), _stray("7")]),
        (D3, D3 + "- D10 A tenth thing\n- D9 A ninth thing\n", [_stray("9"), _stray("10")]),
        (D1, "- D01 Each cause proven against the live ledger, run 4471 green\n", [_stray("01"), _unnamed(1)]),
        (
            D1,
            D1 + "  - D1 its proof is run 4471\n",
            ["deliverable D1 is named by 2 bullets; name it once, under Done or under Pending"],
        ),
    ],
)
def test_each_deliverable_of_the_mission_is_named_once_by_a_done_or_a_pending_bullet(
    old: str, new: str, found: list[str]
) -> None:
    assert _problems(_swap(old, new)) == found


def test_a_handoff_with_nothing_done_names_every_deliverable_as_pending() -> None:
    pending = _swap(D1, "None\n")
    assert _problems(_swap(D2, "- D1 Each cause, two of three proven\n" + D2, pending)) == []
    assert _problems(_swap(D2 + D3, "None\n", pending)) == [_unnamed(1), _unnamed(2), _unnamed(3)]


@pytest.mark.feature("expert-handoff-contract")
@pytest.mark.parametrize(
    "old,new,found",
    [
        (D1, "- D1 Each cause proven against the live ledger\n", [_unevidenced("Done")]),
        (TESTED, "- Seam one went red then green\n", [_unevidenced("Tests")]),
        (VALIDATED, "- The deployed tick claimed the batch\n", [_unevidenced("Validation")]),
        (TESTED, "- Seam one probably passes, hypothesis\n", []),
        (VALIDATED, "None\n", []),
        (
            TESTED,
            TESTED + "- Seam two went green\n",
            ["Tests bullet 2 carries no evidence; add it or label the bullet hypothesis"],
        ),
    ],
)
def test_done_tests_and_validation_bullets_carry_evidence_or_the_word_hypothesis(
    old: str, new: str, found: list[str]
) -> None:
    assert _problems(_swap(old, new)) == found


@pytest.mark.feature("expert-handoff-contract")
def test_each_handoff_fault_is_named_under_the_ten_headings_all_at_once() -> None:
    assert _problems(FAULTY) == [
        "the first line must be # Expert handoff",
        f"## Intent is not one of the ten headings: {HEADINGS}",
        "## Assumptions is missing",
        "## Tests appears more than once",
        f"the headings are out of order; keep {HEADINGS}",
        "## Tests is empty; write None when there is nothing to say",
        "the completion marker <!-- handoff complete --> must be the last line",
        "Read first lists its addresses as bullets, each starting with a GitHub issue or pull request link, "
        "ledger:, workspace:, recap:, inbox:, seat: or mission:",
        "file path hooks/context/recycle.py; name the module, type or command instead",
        "Done bullet 1 carries no evidence; add it or label the bullet hypothesis",
    ]


def test_the_contract_faults_come_before_the_mission_and_deliverable_faults() -> None:
    text = _swap(D3, "", _swap(f"## Mission\n{LINE}\n", f"## Mission\n{ADDRESS}\n", _swap(TESTED, "- Seam one\n")))
    assert _problems(text) == [_unevidenced("Tests"), OPENS, _unnamed(3)]


def test_a_second_mission_section_is_refused_whichever_of_the_two_opens_with_the_mission_line() -> None:
    twice = "## Mission appears more than once"
    assert _problems(_swap("## Done\n", f"## Mission\n{ADDRESS}\n## Done\n")) == [twice, OPENS]
    assert _problems(_swap(f"## Mission\n{LINE}\n", f"## Mission\n{ADDRESS}\n## Mission\n{LINE}\n")) == [twice]


def test_a_read_first_address_that_does_not_resolve_is_refused() -> None:
    found = expert.problems(VALID, lambda address: address == ADDRESS, "sw", GOOD)
    assert found == ["Read first entry 2: recap:expert-1@sw does not resolve"]


@pytest.mark.feature("expert-handoff-contract")
def test_a_mission_title_that_names_a_file_is_the_runtimes_wording_and_passes_the_wording_rules() -> None:
    mission = GOOD.replace("Keep an ordered batch built", "Split scripts/swarm/cli.py under line 3000")
    line, address = expert_mission.line("sw", mission), expert_mission.address("sw", mission)
    assert line == f"{address} Split scripts/swarm/cli.py under line 3000"
    text = VALID.replace(LINE, line).replace(ADDRESS, address)
    assert _problems(text, mission) == []
    assert _problems(_swap("Slice two of the plan", "Slice two in hooks/context/recycle.py", text), mission) == [
        "file path hooks/context/recycle.py; name the module, type or command instead"
    ]
    assert _problems(_swap("## Stopped at\n", f"## Stopped at\n{line}\n", text), mission) == []
