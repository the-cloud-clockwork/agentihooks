"""The opening prompt of an expert: its lineage, its one mission, its obligation and the commands of its loop."""

import json
from typing import Any

import pytest

from scripts.swarm import prompt
from tests import command_lines

pytestmark = pytest.mark.unit

NAME, SUCCESSOR = "expert@a1b2c3-0001", "expert@a1b2c3-0002"
MISSION = "mission:sw/" + "ab" * 32
FIRST: dict[str, Any] = {
    "id": "expert-1",
    "title": "Keep an ordered batch built",
    "mission": MISSION,
    "generation": 1,
    "seat": "expert-1@sw",
}
ME = "agentihooks swarm sw"
OPENING = (
    f"You are {NAME}, an expert in swarm sw, working in the repo /repo. You are expert number 1 of lineage expert-1 "
    "and hold its seat.\n"
    f"Your one mission is: Keep an ordered batch built. It is saved as {MISSION}, and every expert of the lineage "
    f"reads that same text. Read it before anything else: {ME} expert mission. The operator's words in it win over "
    "any reading of them.\n"
    "You claim no ledger task and file no follow up. You complete every instruction of the mission: no exception, no "
    "workaround, no excuse. A problem you meet is solved at its root, inside the mission.\n"
)
NO_HISTORY = (
    "Your seat expert-1@sw has no history yet: no handoff document, no recap and no learned notes.\n"
    "Run agentihooks seat brief before your first command: it prints your seat's handoff, culture, latest recap and "
    "learned notes from the store that keeps them.\n"
)
LOOP = (
    "\n"
    "Your loop is the swarm-expert skill: every change goes through the dev cycle, a pull request is queued on green "
    f"checks with {ME} merge queue <url>, and a decision the mission leaves open goes to the master, never to the "
    'operator: agentihooks msg send master@sw "<plain words>".\n'
    f"Your stop is refused while your lineage runs. While you wait on something, declare it: {ME} wait --on checks "
    f'<url>, {ME} wait --on merge <url>, {ME} wait --on reply <id>, or {ME} wait 30 --reason "<what>".\n'
    'Messages for you arrive in this session as inbox messages: answer one with agentihooks msg reply <id> "<text>" '
    'or close it with agentihooks msg close <id> done "<where the work went>".\n'
    f'Record a lesson the next expert of your seat should know with {ME} learned "<lesson because reason>".\n'
    "\n"
    "If your context nears its limit a hook tells you to write a handoff: use the expert-handoff skill for the expert "
    f"handoff, run {ME} handoff <doc> and stop. The next expert of your lineage continues from it.\n"
    "Your lineage ends only when every deliverable is delivered, validated on the deployed result, smoke tested and "
    "regression tested, and held by a Semgrep rule and a feature map entry. Then write the mission proof and run "
    f"{ME} expert complete <proof>. It refuses a proof with a deliverable unproven or anything left open; a refusal "
    "is open work, not a wording problem. Only an accepted proof ends the lineage, and the swarm then closes this "
    "session.\n"
    "Write messages in plain words: no ids, paths, hashes or dashes.\n"
)
COMMANDS = (
    f"{ME} expert mission",
    f"{ME} merge queue <url>",
    'agentihooks msg send master@sw "<plain words>"',
    f"{ME} wait --on checks <url>",
    f"{ME} wait --on merge <url>",
    f"{ME} wait --on reply <id>",
    f'{ME} wait 30 --reason "<what>"',
    'agentihooks msg reply <id> "<text>"',
    'agentihooks msg close <id> done "<where the work went>"',
    f'{ME} learned "<lesson because reason>"',
    f"{ME} handoff <doc>",
    f"{ME} expert complete <proof>",
)
HANDOFF = (
    "# Expert handoff\n## Mission\n" + MISSION + " Keep an ordered batch built\n## Done\n- D1 Proven, run 4471\n"
    "## Pending\n- D2 Half built\n## Assumptions\nNone\n## Tests\nNone\n## Validation\nNone\n"
    "## Stopped at\nSlice two.\n## Decisions and promises\nNone\n## Next\n1. Finish slice two.\n"
    "## Read first\n- " + MISSION + " the mission and its deliverables\n<!-- handoff complete -->\n"
)
ENVELOPE = {"seat": "expert-1@sw", "agent": NAME, "reason": "recycle", "task": "expert-1"}
TRANSFER = {"id": "9f2c", "handoff": HANDOFF, "next": "1. Finish slice two."}


@pytest.mark.feature("expert-role-profile")
def test_the_first_expert_of_a_lineage_is_told_its_mission_its_obligation_and_its_loop() -> None:
    assert prompt.build("sw", "/repo", "expert", NAME, FIRST) == OPENING + NO_HISTORY + LOOP


@pytest.mark.feature("expert-role-profile")
def test_a_successor_is_told_to_confirm_the_handoff_it_inherits_and_carries_it() -> None:
    task = {**FIRST, "generation": 2, "handoff": HANDOFF, "handoff_envelope": ENVELOPE, "transfer": TRANSFER}
    built = prompt.build("sw", "/repo", "expert", SUCCESSOR, task)

    assert built.startswith(OPENING.replace(NAME, SUCCESSOR).replace("expert number 1", "expert number 2"))
    assert built.endswith(LOOP)
    middle = built[len(OPENING) : -len(LOOP)]
    for carried in (
        "Your seat expert-1@sw carries what earlier occupants left. Read it in this order:\n",
        "Read this handoff before continuing. Confirm you read it and will take its first Next action by running "
        f"{ME} confirm-handoff 9f2c --next '1. Finish slice two.'\n{HANDOFF}",
        "1. Handoff runtime envelope:\n" + json.dumps(ENVELOPE, indent=2) + "\n",
        "Handoff document: continue from it:\n" + HANDOFF,
        "Read first: Open each address in rank order and answer its question before reading older recaps.\n"
        f"- {MISSION} the mission and its deliverables\n",
    ):
        assert carried in middle, carried
    assert NO_HISTORY not in built


def test_an_expert_prompt_names_no_task_claim_and_no_ledger_crew_step() -> None:
    built = prompt.build("sw", "/repo", "expert", NAME, FIRST)
    for absent in ("Your one task", " join", "followup add", f"{ME} done", f"{ME} block", "agentihooks api"):
        assert absent not in built, absent


def test_every_command_an_expert_prompt_names_is_one_the_command_line_takes() -> None:
    built = prompt.build("sw", "/repo", "expert", NAME, FIRST)
    for command in COMMANDS:
        assert command in built, command
        assert command_lines.taken(command), command
    assert built.count("agentihooks ") == len(COMMANDS)


@pytest.mark.feature("expert-role-profile")
def test_the_autonomy_and_the_role_argument_do_not_change_an_expert_prompt() -> None:
    plain = prompt.build("sw", "/repo", "expert", NAME, FIRST)
    assert prompt.build("sw", "/repo", "expert", NAME, FIRST, role="an engineer", autonomy="full") == plain
    assert prompt.build_expert("sw", "/repo", NAME, FIRST) == plain


def test_a_carried_opening_is_returned_as_it_is_for_an_expert() -> None:
    assert prompt.build("sw", "/repo", "expert", NAME, {"hive_opening": "carried opening"}) == "carried opening"
