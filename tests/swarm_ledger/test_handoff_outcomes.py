import json
import subprocess

import pytest

from scripts.swarm_ledger.ledger_server import control_argv
from tests.swarm_ledger.test_swarm_panel import function_source

pytestmark = pytest.mark.unit


def render(name, row):
    script = (
        "const pending = ''; const h = (tag, attrs, ...kids) => ({tag, ...attrs, kids: kids.filter(Boolean)});"
        + function_source(name)
        + f"\nprocess.stdout.write(JSON.stringify({name}({json.dumps(row)})));"
    )
    return json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)


def test_transfer_card_labels_the_two_independent_outcomes():
    card = render(
        "transferCard",
        {
            "seat": "eng-1@sw",
            "reason": "recycle",
            "task": "Verify handoff",
            "continuity": {"state": "pending"},
            "binding": {"state": "live"},
        },
    )
    text = json.dumps(card)
    assert "Continuity: pending" in text
    assert "Binding: live" in text


def test_failed_restore_offers_resume_and_fresh_on_its_card():
    card = render(
        "restoreCard",
        {
            "name": "sw-eng-1",
            "task": "Verify restore",
            "outcome": "awaiting-decision",
            "reason": "Conversation unavailable",
        },
    )
    text = json.dumps(card)
    assert "Awaiting decision" in text
    assert '"text": "Resume"' in text
    assert '"text": "Fresh"' in text
    assert '"data-agent": "sw-eng-1"' in text


@pytest.mark.parametrize("choice", ["resume", "fresh"])
def test_page_restore_choice_routes_to_the_matching_command(choice):
    assert control_argv({"action": "restore-decision", "agent": "engineer@abc123-0001", "choice": choice}) == [
        "restore-decision",
        "engineer@abc123-0001",
        choice,
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"action": "restore-decision", "agent": "--as", "choice": "fresh"},
        {"action": "restore-decision", "agent": "sw-eng-1", "choice": "automatic"},
    ],
)
def test_bad_restore_choices_are_refused(body):
    with pytest.raises(ValueError):
        control_argv(body)
