import json

import pytest

from scripts.swarm import cli
from tests.swarm.test_cli import env, run

_fixture = env
CI = "ci@a1b2c3-0001"
ESCALATED = {
    "attempts": [{"after_critic": False}, {"after_critic": False}, {"after_critic": True}],
    "critic": {"resolution": "scale the pool"},
    "escalation": {"reason": "the pool quota is spent"},
}


@pytest.fixture
def started(env):
    store, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    return store, ledger


def test_a_block_at_delegate_without_the_protocol_is_refused(started, capsys):
    _, ledger = started
    capsys.readouterr()
    assert run("sw", "--as", CI, "block", "waiting on a token") == 1
    assert capsys.readouterr().err == (
        "swarm: at delegate autonomy a block reaches the operator only after the blocker protocol, so the next step "
        'is to attempt 1 of 2: agentihooks swarm sw resolve attempt tasks/t2 --tried "<what you tried>" '
        '--command "<the command run>" --output "<its output>"\n'
    )
    assert ledger.rows["t2"].get("state") != "blocked"


def test_a_block_after_the_escalation_lands(started):
    _, ledger = started
    ledger.rows["t2"]["blocker"] = ESCALATED
    assert run("sw", "--as", CI, "block", "waiting on a token") == 0
    assert ledger.rows["t2"]["state"] == "blocked"


def test_a_block_at_assist_needs_no_record(started):
    store, ledger = started
    store.update("sw", autonomy="assist")
    assert run("sw", "--as", CI, "block", "waiting on a token") == 0
    assert ledger.rows["t2"]["state"] == "blocked"


def test_a_block_reads_its_task_and_dependencies_by_id_never_the_collection(started, monkeypatch, capsys):
    store, ledger = started
    store.update("sw", autonomy="assist")
    ledger.rows["t2"]["depends_on"] = ["t1"]
    ledger.rows["t1"]["state"] = "done"
    monkeypatch.setattr(ledger, "tasks", lambda slug: pytest.fail("block read the whole tasks collection"))
    assert run("sw", "--as", CI, "block", "waiting on a token") == 0
    assert ledger.rows["t2"]["state"] == "blocked"


def test_a_block_waits_on_a_dependency_the_ledger_no_longer_holds(started, capsys):
    store, ledger = started
    store.update("sw", autonomy="assist")
    ledger.rows["t2"]["depends_on"] = ["t9"]
    capsys.readouterr()
    assert run("sw", "--as", CI, "block", "waiting on a token") == 0
    assert json.loads(capsys.readouterr().out)["waits_on"]["target"] == "t9"
    assert ledger.rows["t2"].get("state") != "blocked"


def test_a_block_without_its_task_on_the_ledger_is_a_named_refusal(started, capsys):
    store, ledger = started
    store.update("sw", autonomy="assist")
    del ledger.rows["t2"]
    capsys.readouterr()
    assert run("sw", "--as", CI, "block", "waiting on a token") == 1
    assert capsys.readouterr().err == "swarm: ledger sw has no task t2\n"


@pytest.mark.parametrize("action", ["attempt", "critic", "escalate", "status"])
def test_resolve_parses_each_step_with_empty_defaults(action):
    parsed = cli.build_parser().parse_args(["sw", "resolve", action, "tasks/t2"])
    assert (parsed.command, parsed.action, parsed.item) == ("resolve", action, "tasks/t2")
    assert [parsed.tried, parsed.ran, parsed.output, parsed.reason, parsed.evidence] == [""] * 5


def test_resolve_refuses_an_unknown_step_and_reads_every_flag():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["sw", "resolve", "skip", "tasks/t2"])
    flags = ["--tried", "a", "--command", "b", "--output", "c", "--reason", "d", "--evidence", "e"]
    parsed = cli.build_parser().parse_args(["sw", "resolve", "escalate", "tasks/t2", *flags])
    assert [parsed.tried, parsed.ran, parsed.output, parsed.reason, parsed.evidence] == ["a", "b", "c", "d", "e"]


class Ledger:
    def __init__(self):
        self.writes = []

    def item(self, slug, path):
        return {"id": "t2", "title": "Ship it"} if (slug, path) == ("sw", "tasks/t2") else None

    def blocker(self, slug, kind, by, item, fields):
        self.writes.append((slug, kind, by, item, fields))


def test_resolve_records_a_step_as_the_calling_agent(started, monkeypatch, capsys):
    fake = Ledger()
    monkeypatch.setattr(cli, "LedgerClient", lambda: fake)
    capsys.readouterr()
    argv = ("resolve", "attempt", "tasks/t2", "--tried", "restarted", "--command", "kubectl get pods", "--output", "ok")
    assert run("sw", "--as", CI, *argv) == 0
    fields = {"text": "restarted", "command": "kubectl get pods", "output": "ok"}
    assert fake.writes == [("sw", "blocker_attempt", CI, "tasks/t2", fields)]
    assert json.loads(capsys.readouterr().out)["autonomy"] == "delegate"
