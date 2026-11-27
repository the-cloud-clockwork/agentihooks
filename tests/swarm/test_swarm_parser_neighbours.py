"""The swarm command parser around the place a new verb is registered: its neighbours parse as they did."""

import pytest

from scripts.swarm import cli

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "argv,field,value",
    [
        (["sw", "culture", "show"], "action", "show"),
        (["sw", "culture", "set", "notes.md"], "file", "notes.md"),
        (["sw", "masters-channel", "read"], "action", "read"),
        (["sw", "master", "up"], "action", "up"),
        (["sw", "say", "hi", "--to", "eng"], "text", "hi"),
        (["sw", "say", "hi", "--to", "eng"], "to", "eng"),
        (["sw", "learned", "a note because it matters"], "command", "learned"),
        (["sw", "--as", "engineer@a1b2c3-0001", "status"], "name", "engineer@a1b2c3-0001"),
    ],
)
def test_a_command_beside_a_new_verb_still_parses(argv: list[str], field: str, value: str) -> None:
    assert getattr(cli.build_parser().parse_args(argv), field) == value
