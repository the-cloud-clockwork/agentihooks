import pytest

from scripts.swarm import cli
from scripts.swarm_ledger import ledger_server

pytestmark = pytest.mark.unit

CHOICES = ("resume", "native", "context", "fresh")


@pytest.mark.parametrize("choice", CHOICES)
def test_the_command_line_takes_every_recovery_choice(choice):
    args = cli.build_parser().parse_args(["sw", "restore-decision", "eng-7", choice])
    assert (args.command, args.agent, args.choice) == ("restore-decision", "eng-7", choice)


@pytest.mark.parametrize("choice", ["later", "NATIVE", ""])
def test_the_command_line_refuses_any_other_choice(choice):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["sw", "restore-decision", "eng-7", choice])


@pytest.mark.parametrize("choice", CHOICES)
def test_the_ledger_server_passes_every_recovery_choice_on(choice):
    assert ledger_server.restore_decision_argv({"agent": "eng-7", "choice": choice}) == [
        "restore-decision",
        "eng-7",
        choice,
    ]


@pytest.mark.parametrize("choice", ["later", "NATIVE", "CONTEXT", None])
def test_the_ledger_server_refuses_any_other_choice_by_name(choice):
    with pytest.raises(ValueError) as refused:
        ledger_server.restore_decision_argv({"agent": "eng-7", "choice": choice})
    assert str(refused.value) == "Choose native, context, fresh or resume"
