import pytest

from scripts.swarm_ledger.ledger_server import control_argv

pytestmark = pytest.mark.unit


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
