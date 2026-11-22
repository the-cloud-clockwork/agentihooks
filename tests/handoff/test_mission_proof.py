"""The mission proof: the document a lineage ends on, with every deliverable proven and nothing left open."""

import pytest

from scripts.handoff import check, expert
from scripts.swarm import expert_mission
from tests.swarm.test_expert_mission import GOOD

pytestmark = pytest.mark.unit

LINE = expert_mission.line("sw", GOOD)
ADDRESS = expert_mission.address("sw", GOOD)
HEADINGS = "Mission, Deliverables, Validation, Smoke tests, Regression tests, Semgrep rules, Feature map, Open items"
UNSHOWN = "carries no evidence; add the run, command or link that shows it"
GUESSED = "is labelled hypothesis; a proof holds only what was shown"
STRAY = "Deliverables bullet {number} names no deliverable; start it with D and its number"
PLAN, FIRST, SECOND = (f"https://github.com/o/r/pull/{number}" for number in (12, 14, 15))
D1 = "- D1 Each cause proven against the live ledger, run 4471\n"
D2 = f"- D2 The plan file, merged in {PLAN}\n"
D3 = f"- D3 The implementation, merged in {FIRST} and {SECOND}\n"
VALIDATED = "- The deployed tick claimed the batch, `agentihooks swarm sw status` printed 3 claims\n"
SMOKED = "- The released command ran on the controller, run 4480\n"
REGRESSED = "- The batch claim tests are green on dev, run 4481\n"
RULED = "- Rule feature-batch-claim holds on dev, run 4482\n"
MAPPED = "- Entry batch-claim graded with its live check, run 4483\n"
PROVEN = {
    "Validation": VALIDATED,
    "Smoke tests": SMOKED,
    "Regression tests": REGRESSED,
    "Semgrep rules": RULED,
    "Feature map": MAPPED,
}
VALID = f"""# Mission proof
## Mission
{LINE}
## Deliverables
{D1}{D2}{D3}## Validation
{VALIDATED}## Smoke tests
{SMOKED}## Regression tests
{REGRESSED}## Semgrep rules
{RULED}## Feature map
{MAPPED}## Open items
None
<!-- mission complete -->
"""
FAULTY = f"""# Expert handoff
## Mission
{LINE}
## Validation
- Checked by hand
## Deliverables
- D1 Each cause proven, run 4471
- D2 The plan file, run 4472
- D3 The implementation, run 4473
## Pending
- D3 one more pass
## Smoke tests
## Regression tests
- Green on dev, run 4481
## Semgrep rules
- Rule holds, run 4482
## Feature map
- Entry graded, run 4483
<!-- handoff complete -->
"""


def _swap(old: str, new: str, text: str = VALID) -> str:
    assert old in text
    return text.replace(old, new, 1)


def _problems(text: str, unmerged: tuple[str, ...] = ()) -> list[str]:
    return expert.proof_problems(text, lambda address: True, lambda url: url not in unmerged, "sw", GOOD)


def _unnamed(number: int) -> str:
    return f"deliverable D{number} is named by no Deliverables bullet; start one with D{number}"


def _stranger(number: str) -> str:
    return f"D{number} names no deliverable, the mission has 3; number the bullet after the deliverable it proves"


@pytest.mark.feature("expert-lineage-ends-on-proof")
def test_a_proof_of_every_deliverable_with_nothing_open_has_no_problems() -> None:
    assert _problems(VALID) == []


@pytest.mark.feature("expert-lineage-ends-on-proof")
@pytest.mark.parametrize(
    "old,new,found",
    [
        (D3, "", [_unnamed(3)]),
        (D1, "- Each cause proven for D1, run 4471\n", [STRAY.format(number=1), _unnamed(1)]),
        (D2, D2 + D2, ["deliverable D2 is named by 2 bullets; name it once under Deliverables"]),
        (D3, D3 + "- D4 A fourth thing, run 4474\n", [_stranger("4")]),
        (D3, D3 + "- D0 Nothing, run 4475\n", [_stranger("0")]),
        (D3, D3 + "- D10 A tenth thing, run 4476\n- D9 A ninth thing, run 4477\n", [_stranger("9"), _stranger("10")]),
        (D1, "- D01 Each cause proven against the live ledger, run 4471\n", [_stranger("01"), _unnamed(1)]),
        (D1, "- D1 Each cause proven against the live ledger\n", [f"Deliverables bullet 1 {UNSHOWN}"]),
        (D2, "- D2 The plan file, a hypothesis until it merges\n", [f"Deliverables bullet 2 {GUESSED}"]),
        (D2, f"- D2 Hypothesis: the plan file merged in {PLAN}\n", [f"Deliverables bullet 2 {GUESSED}"]),
    ],
)
def test_each_deliverable_of_the_mission_is_proven_by_one_evidenced_bullet(
    old: str, new: str, found: list[str]
) -> None:
    assert _problems(_swap(old, new)) == found


@pytest.mark.feature("expert-lineage-ends-on-proof")
@pytest.mark.parametrize("heading", list(PROVEN))
def test_each_check_section_holds_at_least_one_evidenced_bullet(heading: str) -> None:
    assert _problems(_swap(PROVEN[heading], "None\n")) == [
        f"## {heading} holds no bullet; name at least one check with its evidence"
    ]
    assert _problems(_swap(PROVEN[heading], "Checked, run 4490.\n")) == [
        f"## {heading} holds no bullet; name at least one check with its evidence"
    ]
    assert _problems(_swap(PROVEN[heading], "- Checked by hand\n")) == [f"{heading} bullet 1 {UNSHOWN}"]
    guessed = PROVEN[heading] + "- A hypothesis: it also holds under load, run 4491\n"
    assert _problems(_swap(PROVEN[heading], guessed)) == [f"{heading} bullet 2 {GUESSED}"]


@pytest.mark.feature("expert-lineage-ends-on-proof")
@pytest.mark.parametrize("left", ["- D3 needs one more pass", "One doc line is missing.", "none", "None."])
def test_a_proof_with_anything_left_open_is_refused(left: str) -> None:
    assert _problems(_swap("## Open items\nNone\n", f"## Open items\n{left}\n")) == [
        "## Open items must be None; a lineage ends with nothing open"
    ]


def test_an_empty_open_items_section_is_told_to_say_none_once() -> None:
    assert _problems(_swap("## Open items\nNone\n", "## Open items\n\n")) == [
        "## Open items is empty; write None when there is nothing to say"
    ]


@pytest.mark.feature("expert-lineage-ends-on-proof")
def test_every_pull_request_named_under_deliverables_is_merged() -> None:
    assert _problems(VALID, unmerged=(FIRST,)) == [f"pull request {FIRST} is not merged"]
    assert _problems(VALID, unmerged=(SECOND, PLAN)) == [
        f"pull request {PLAN} is not merged",
        f"pull request {SECOND} is not merged",
    ]
    elsewhere = _swap(VALIDATED, f"- Reviewed in https://github.com/o/r/pull/99, run 4484\n{VALIDATED}")
    assert _problems(elsewhere, unmerged=("https://github.com/o/r/pull/99",)) == []
    assert _problems(_swap(D2, D2.replace("merged in", f"merged in {PLAN} then")), unmerged=(PLAN,)) == [
        f"pull request {PLAN} is not merged"
    ]


@pytest.mark.feature("expert-lineage-ends-on-proof")
@pytest.mark.parametrize(
    "written",
    [
        "#13200",
        "http://github.com/o/r/pull/7",
        "https://www.github.com/o/r/pull/7",
        "github.com/o/r/pull/7",
        "https://github.com/o/r/issues/7",
    ],
)
def test_a_pull_request_under_deliverables_is_written_as_the_link_the_swarm_can_read(written: str) -> None:
    assert _problems(_swap(D1, D1.rstrip("\n") + f", merged in {written}\n")) == [
        f"{written} under Deliverables cannot be read as a merged pull request; write it as "
        "https://github.com/<owner>/<repo>/pull/<number>"
    ]


def test_a_reference_outside_deliverables_is_not_held_to_the_link_form() -> None:
    assert _problems(_swap(SMOKED, SMOKED.rstrip("\n") + ", see #13200 and github.com/o/r/pull/7\n")) == []


def test_a_pull_request_link_with_a_page_behind_it_is_read_as_that_pull_request() -> None:
    text = _swap(D2, f"- D2 The plan file, merged in {PLAN}/files\n")
    assert _problems(text) == []
    assert _problems(text, (PLAN,)) == [f"pull request {PLAN} is not merged"]


@pytest.mark.parametrize("opened", [ADDRESS, expert_mission.line("other", GOOD), "None"])
def test_the_mission_section_opens_with_the_mission_line_of_the_lineage(opened: str) -> None:
    assert _problems(_swap(f"## Mission\n{LINE}\n", f"## Mission\n{opened}\n")) == [
        f"## Mission must open with the mission line: {LINE}"
    ]


@pytest.mark.feature("expert-lineage-ends-on-proof")
def test_each_fault_of_a_proof_is_named_under_its_eight_headings_all_at_once() -> None:
    assert _problems(FAULTY, unmerged=()) == [
        "the first line must be # Mission proof",
        f"## Pending is not one of the eight headings: {HEADINGS}",
        "## Open items is missing",
        f"the headings are out of order; keep {HEADINGS}",
        "## Smoke tests is empty; write None when there is nothing to say",
        "the completion marker <!-- mission complete --> must be the last line",
        f"Validation bullet 1 {UNSHOWN}",
        "## Smoke tests holds no bullet; name at least one check with its evidence",
    ]


def test_a_handoff_marker_does_not_end_a_proof() -> None:
    assert _problems(_swap("<!-- mission complete -->", "<!-- handoff complete -->")) == [
        "the completion marker <!-- mission complete --> must be the last line"
    ]


def test_a_handoff_v2_that_ends_on_the_proof_marker_is_told_its_own_marker_and_nothing_else() -> None:
    handoff = (
        "# Handoff v2\n## Intent\n- Refuse a malformed handoff\n## Done\nNone\n## Stopped at\n- Seam two\n"
        "## Decisions and promises\n- Keep the cache out\n## Next\n- Run it\n## Read first\nNone\n"
        "<!-- mission complete -->\n"
    )
    assert check.problems(handoff, lambda address: False) == [
        "the completion marker <!-- handoff complete --> must be the last line"
    ]


@pytest.mark.feature("expert-lineage-ends-on-proof")
@pytest.mark.parametrize("heading", ["Deliverables", *PROVEN])
def test_the_word_proof_alone_shows_nothing_in_a_mission_proof(heading: str) -> None:
    shown = D1 if heading == "Deliverables" else PROVEN[heading]
    bare = "- D1 Each cause is fixed, proof attached\n" if heading == "Deliverables" else "- It holds, see the proof\n"
    assert _problems(_swap(shown, bare)) == [f"{heading} bullet 1 {UNSHOWN}"]


def test_the_word_proof_still_stands_in_a_handoff() -> None:
    handoff = (
        "# Handoff v2\n## Intent\n- Refuse a malformed handoff\n## Done\n- Seam one is green, see the proof\n"
        "## Stopped at\n- Seam two\n## Decisions and promises\nNone\n## Next\n- Run it\n## Read first\nNone\n"
        "<!-- handoff complete -->\n"
    )
    assert check.problems(handoff, lambda address: False) == []


@pytest.mark.feature("expert-lineage-ends-on-proof")
@pytest.mark.parametrize(
    "stray", ["- D1a One more note, run 4476\n", "- The plan held up, run 4477\n", "- d2 again, run 4478\n"]
)
def test_every_deliverables_bullet_names_a_deliverable(stray: str) -> None:
    assert _problems(_swap(D3, D3 + stray)) == [STRAY.format(number=4)]


@pytest.mark.feature("expert-lineage-ends-on-proof")
def test_a_marker_shaped_line_inside_a_section_is_content_and_hides_nothing() -> None:
    hidden = _swap("## Open items\nNone\n", "## Open items\n<!-- mission complete -->\n- D3 needs one more pass\n")
    assert _problems(hidden) == ["## Open items must be None; a lineage ends with nothing open"]
    inside = _swap("## Open items\nNone\n", "## Open items\n<!-- mission complete -->\nNone\n")
    assert _problems(inside) == ["## Open items must be None; a lineage ends with nothing open"]
    assert check.section(inside, "Open items") == "<!-- mission complete -->\nNone"
    handoff = (
        "# Handoff v2\n## Intent\n- Refuse a malformed handoff\n## Done\nNone\n## Stopped at\n- Seam two\n"
        "## Decisions and promises\nNone\n## Next\n<!-- handoff complete -->\n- Run it\n## Read first\nNone\n"
        "<!-- handoff complete -->\n"
    )
    assert check.section(handoff, "Next") == "<!-- handoff complete -->\n- Run it"
    assert check.section(handoff, "Read first") == "None"


def test_a_mission_title_that_names_a_file_passes_the_wording_rules() -> None:
    mission = GOOD.replace("Keep an ordered batch built", "Split scripts/swarm/cli.py under line 3000")
    text = VALID.replace(LINE, expert_mission.line("sw", mission))
    assert expert.proof_problems(text, lambda address: True, lambda url: True, "sw", mission) == []
    pathed = _swap("The deployed tick", "The tick in hooks/context/recycle.py", text)
    assert expert.proof_problems(pathed, lambda address: True, lambda url: True, "sw", mission) == [
        "file path hooks/context/recycle.py; name the module, type or command instead"
    ]
