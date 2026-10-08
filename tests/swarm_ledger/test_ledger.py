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
    ["phase", "p1", "open"],
    ["phase", "set", "p1", "planning=auto"],
    ["followup", "add", "Tests pass"],
    ["followup", "done", "f1"],
    ["followup", "open", "f1"],
    ["followup", "flag", "f1"],
    ["followup", "unflag", "f1"],
    ["question", "add", "Which port?"],
    ["scope", "tasks/t1", "out"],
    ["scope", "tasks/t1", "in"],
    ["edit", "chat", "m1", "Tests pass"],
    ["delete", "chat", "m1"],
    ["priority", "add", "tasks/t1", "Merge it?"],
    ["priority", "clear", "p1"],
    ["priority", "clear", "--all"],
    ["alert", "claim", "a1"],
    ["alert", "close", "a1", "Fixed"],
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
    monkeypatch.setattr(ledger_phase_cli, "append_phases", lambda plan, taken: [{"phase": "p9", "planning": "auto"}])

    def run(argv, reply):
        sent = []

        def request(slug, ops=None, service=False):
            assert slug == "s"
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


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["delete", "chat", "m1"], "delete on chat"),
        (["followup", "done", "f9"], "set on followups/f9/done"),
        (["priority", "clear", "p1"], "priority_clear on p1"),
        (["claim", "tasks/t9"], "claim on tasks/t9"),
        (["leave"], "leave on the ledger"),
        (["question", "add", "Which port?"], "add_item on questions"),
        (["say", "Hi"], "add on chat"),
        (["comment", "tasks/t1", "Hi"], "add on tasks/t1/comments"),
        (["edit", "tasks/t1", "c1", "Hi"], "edit on tasks/t1/comments"),
    ],
)
def test_a_refusal_without_a_reason_names_each_refused_operation(cli, argv, message):
    with pytest.raises(SystemExit) as stop:
        cli(argv, lambda ids: {"rejected": ids, "_meta": {"warnings": []}})
    assert stop.value.code == (
        f"{message} refused without a reason from the server: "
        "check that the entry exists, that you may change it and that its text is not empty"
    )


def test_a_refusal_of_unsent_operations_names_their_ids():
    with pytest.raises(SystemExit) as stop:
        ledger.refused({"rejected": ["x"]}, [{"op": "add", "id": "y", "thread": "chat"}])
    assert stop.value.code == "rejected: ['x']"


def test_a_refused_artifact_without_a_reason_says_to_join_and_name_a_task(cli):
    with pytest.raises(SystemExit) as stop:
        cli(["artifact", "PLAN", "Plan"], lambda ids: {"rejected": ids})
    assert stop.value.code == "rejected: join the ledger first and name a task it holds"


@pytest.mark.parametrize(
    ("argv", "entry"),
    [
        (["say", " Hi "], {"op": "add", "thread": "chat", "text": "Hi", "by": "eng-1"}),
        (["comment", "tasks/t1", "Hi"], {"op": "add", "thread": "tasks/t1/comments", "text": "Hi", "by": "eng-1"}),
        (["edit", "chat", "m1", "Hi"], {"op": "edit", "thread": "chat", "id": "m1", "text": "Hi", "by": "eng-1"}),
    ],
)
def test_text_commands_send_their_entry(cli, capsys, argv, entry):
    (sent,) = cli(argv, applied)
    assert {key: sent[key] for key in entry} == entry
    assert capsys.readouterr().out


@pytest.mark.parametrize("argv", [["say", "Hi"], ["comment", "tasks/t1", "Hi"]], ids=" ".join)
def test_text_commands_print_whether_the_write_landed(cli, capsys, argv):
    cli(argv, applied)
    assert json.loads(capsys.readouterr().out) == {"posted": True}
    with pytest.raises(SystemExit):
        cli(argv, refused)
    assert json.loads(capsys.readouterr().out) == {"posted": False}


def test_a_refusal_leaves_out_the_ledger_size_warnings():
    from scripts.swarm_ledger import ledger_core

    doc = {"overview": "word " * 201, "phases": [{"id": "p1", "description": "word " * 101}]}
    size = ledger_core.warnings(doc)
    assert len(size) == 2 and all(ledger_core.size_warning(w) for w in size)
    assert not ledger_core.size_warning(REASON)
    with pytest.raises(SystemExit) as stop:
        ledger.refused({"rejected": ["x"], "_meta": {"warnings": [*size, REASON]}})
    assert stop.value.code == REASON


@pytest.mark.parametrize(
    ("value", "form"),
    [
        ('proof={"evidence":"E","output":"O"}', "proof.evidence=E proof.output=O"),
        ('contract={"must":"M"}', "contract.must=M contract.check=C"),
    ],
)
def test_task_set_refuses_a_whole_object_naming_the_dotted_form(cli, monkeypatch, value, form):
    name = value.partition("=")[0]
    allowed = ledger.OBJECT_FORMS[name][0]
    sent = []
    monkeypatch.setattr(ledger, "send", lambda *a, **k: sent.append(k))
    with pytest.raises(SystemExit) as stop:
        cli(["task", "set", "t1", value], applied)
    assert sent == []
    assert f"one {name}.KEY=VALUE pair per key among {allowed}, e.g. {form}, not {name}=VALUE" in str(stop.value.code)
