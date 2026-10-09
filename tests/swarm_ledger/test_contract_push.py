import pytest

from scripts.swarm_ledger import ledger, ledger_kinds, ledger_tasks

CONTRACT = {"must": "the cause is shown", "check": "a CI probe", "judge": "the Standards reader"}
PUSH_HELP = "contract: the proof needs a pushed branch and a CI run, so never qa"


@pytest.mark.parametrize("value", ["yes", "no"])
def test_check_takes_push_yes_or_no(value):
    ledger_kinds.check({"contract": {**CONTRACT, "push": value}})


@pytest.mark.parametrize("value", ["Yes", "true", ""])
def test_check_refuses_any_other_push_value(value):
    with pytest.raises(ValueError) as refused:
        ledger_kinds.check({"contract": {"push": value}})
    assert str(refused.value) == "contract push must be yes or no"


@pytest.mark.parametrize("field", ["contract", "proof"])
def test_check_still_refuses_unknown_object_keys(field):
    with pytest.raises(ValueError, match=f"^{field} must be an object of strings"):
        ledger_kinds.check({field: {"who": "x"}})


@pytest.mark.parametrize(
    ("task", "merged"),
    [
        ({"contract": CONTRACT}, {**CONTRACT, "push": "yes"}),
        ({"contract": {**CONTRACT, "push": "no"}}, {**CONTRACT, "push": "yes"}),
        ({"contract": None}, {"push": "yes"}),
        ({}, {"push": "yes"}),
    ],
)
def test_a_contract_update_keeps_the_keys_it_does_not_name(task, merged):
    fields = {"state": "open", "contract": {"push": "yes"}}
    assert ledger_tasks._update_fields(task, fields) == {"state": "open", "contract": merged}


def test_an_update_without_a_contract_is_left_alone():
    assert ledger_tasks._update_fields({"contract": CONTRACT}, {"state": "pr"}) == {"state": "pr"}


def _added(monkeypatch, *flags):
    sent = []
    monkeypatch.setattr(ledger, "send", lambda args, kind, /, **fields: sent.append(fields))
    argv = ["--slug", "s", "--as", "x", "task", "add", "t3", "probe", "--kind", "troubleshoot", *flags]
    args = ledger.build_parser().parse_args(argv)
    ledger.cmd_task(args)
    return args, sent[0]


def test_task_add_push_stores_push_yes_in_the_contract(monkeypatch):
    args, fields = _added(monkeypatch, "--must", CONTRACT["must"], "--push")
    assert (args.push, fields["contract"]) == ("yes", {"must": CONTRACT["must"], "push": "yes"})


def test_task_add_without_push_sends_no_push(monkeypatch):
    args, fields = _added(monkeypatch, "--check", CONTRACT["check"])
    assert (args.push, fields["contract"]) == ("", {"check": CONTRACT["check"]})


def test_task_add_help_names_the_push_contract(capsys):
    with pytest.raises(SystemExit):
        ledger.build_parser().parse_args(["--slug", "s", "task", "add", "--help"])
    assert f" {PUSH_HELP} " in f" {' '.join(capsys.readouterr().out.split())} "
