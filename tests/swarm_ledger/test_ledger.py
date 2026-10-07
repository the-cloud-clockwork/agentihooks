import json

import pytest

from scripts.swarm_ledger import ledger

REASON = "tasks/t1 cannot be done without its proof: proof.command, proof.output"

WRITES = [
    ["join"],
    ["leave"],
    ["ack", "--rev", "1"],
    ["say", "Tests pass"],
    ["comment", "tasks/t1", "Tests pass"],
    ["retext", "followups/f1", "Tests pass"],
    ["artifact", "PLAN", "Plan"],
    ["artifact-purge"],
    ["publish-plan", "PLAN", "--phase", "p1"],
    ["plan", "phases", "PLAN"],
    ["phase", "p1", "done"],
    ["followup", "add", "Tests pass"],
    ["followup", "done", "f1"],
    ["followup", "flag", "f1"],
    ["question", "add", "Which port?"],
    ["scope", "tasks/t1", "out"],
    ["edit", "chat", "m1", "Tests pass"],
    ["delete", "chat", "m1"],
    ["priority", "add", "tasks/t1", "Merge it?"],
    ["priority", "clear", "p1"],
    ["alert", "claim", "a1"],
    ["relay", "questions/q1", "Yes", "--quote", "yes"],
    ["answer", "questions/q1", "Yes"],
    ["time-left", "30m"],
    ["claim", "tasks/t1"],
    ["task", "add", "t9", "New", "task"],
    ["task", "set", "t1", "state=done"],
]


@pytest.fixture
def cli(monkeypatch, tmp_path):
    import ledger_relay

    from scripts.swarm_ledger import ledger_phase_cli

    plan = tmp_path / "plan.json"
    plan.write_text("[]")
    monkeypatch.setattr(ledger, "upload_artifact", lambda *a: {"id": "f1"})
    monkeypatch.setattr(ledger, "resource", lambda *a, **k: [])
    monkeypatch.setattr(ledger, "swarm_autonomy", lambda slug: "full")
    monkeypatch.setattr(ledger_relay, "verified", lambda name, quote: True)
    monkeypatch.setattr(ledger.ledger_publish, "publish", lambda path, title, repo, artifact: ("https://x/1", "issue"))
    monkeypatch.setattr(ledger_phase_cli, "append_phases", lambda plan, taken: [{"phase": "p9"}])

    def run(argv, reply):
        sent = []

        def request(slug, ops=None, service=False):
            sent.extend(ops or [])
            ids = [op["id"] for op in ops or []]
            return reply(ids)

        monkeypatch.setattr(ledger, "request", request)
        argv = [str(plan) if part == "PLAN" else part for part in argv]
        args = ledger.build_parser().parse_args(["--slug", "s", "--as", "eng-1", *argv])
        getattr(ledger, f"cmd_{args.command.replace('-', '_')}")(args)
        return sent

    return run


def refused(ids):
    return {"applied": [], "rejected": ids, "_meta": {"rev": 2, "warnings": [REASON]}}


def applied(ids):
    return {"applied": ids, "rejected": [], "_meta": {"rev": 2, "warnings": []}, "tasks": []}


@pytest.mark.parametrize("argv", WRITES, ids=" ".join)
def test_every_write_command_exits_non_zero_naming_the_refusal_reason(cli, capsys, argv):
    with pytest.raises(SystemExit) as stop:
        cli(argv, refused)
    assert stop.value.code not in (0, None)
    assert REASON in f"{stop.value.code}{capsys.readouterr().err}"


@pytest.mark.parametrize("argv", WRITES, ids=" ".join)
def test_every_write_command_prints_an_applied_write_and_exits_zero(cli, capsys, argv, monkeypatch):
    purged = [{"id": "p0", "kind": "artifacts purged", "count": 0}]
    monkeypatch.setattr(ledger, "resource", lambda s, p, **k: {"artifacts": 0} if p == "counts" else purged)
    cli(argv, applied)
    out = capsys.readouterr().out
    assert out and REASON not in out
    json.loads(out.splitlines()[-1])


def test_a_refusal_without_a_reason_names_the_refused_operations(cli):
    with pytest.raises(SystemExit) as stop:
        cli(["delete", "chat", "m1"], lambda ids: {"rejected": ids, "_meta": {"warnings": []}})
    assert "rejected" in str(stop.value.code)


def test_a_refusal_leaves_out_the_ledger_size_warnings():
    from scripts.swarm_ledger import ledger_core

    doc = {"overview": "word " * 201, "phases": [{"id": "p1", "description": "word " * 101}]}
    size = ledger_core.warnings(doc)
    assert len(size) == 2 and all(ledger_core.size_warning(w) for w in size)
    assert not ledger_core.size_warning(REASON)
    with pytest.raises(SystemExit) as stop:
        ledger.refused({"rejected": ["x"], "_meta": {"warnings": [*size, REASON]}})
    assert stop.value.code == REASON
