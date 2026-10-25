import json
import re
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import ledger, ledger_fold
from tests.swarm_ledger.test_ledger_fold import ledger as make_ledger
from tests.swarm_ledger.test_ledger_fold import plan, task

DOC = make_ledger(task("t1", title="Merge wait reports outcomes", territory=["scripts"]), task("t2"))


@pytest.fixture
def server(monkeypatch):
    sent = {"send": [], "call": [], "refused": []}

    def call(slug, ops=None):
        sent["call"].append((slug, ops))
        return DOC if ops is None else {"rev": len(sent["call"])}

    monkeypatch.setattr(ledger, "call", call)
    monkeypatch.setattr(ledger, "send", lambda args, kind, **fields: sent["send"].append((args.name, kind, fields)))
    monkeypatch.setattr(ledger, "refused", lambda state, ops: sent["refused"].append((state, ops)))
    return sent


def args(action, path=None):
    return SimpleNamespace(slug="demo", name="master@abcdef-0001", action=action, plan=path)


def written(tmp_path, body):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(body))
    return str(path)


def test_the_parser_takes_each_fold_action_and_an_optional_plan():
    parse = ledger.build_parser().parse_args
    found = parse(["--slug", "demo", "--as", "n", "fold", "check", "plan.json"])
    assert (found.command, found.action, found.plan) == ("fold", "check", "plan.json")
    assert parse(["--slug", "demo", "--as", "n", "fold", "candidates"]).plan is None
    assert parse(["--slug", "demo", "--as", "n", "fold", "apply", "p"]).action == "apply"
    with pytest.raises(SystemExit):
        parse(["--slug", "demo", "--as", "n", "fold", "merge"])


def test_the_fold_command_hands_its_arguments_to_the_fold_module(monkeypatch):
    seen = []
    monkeypatch.setattr(ledger_fold, "run", seen.append)
    given = args("check")
    ledger.cmd_fold(given)
    assert seen == [given]


def test_candidates_print_the_candidate_groups(server, capsys):
    ledger_fold.run(args("candidates"))
    assert capsys.readouterr().out == json.dumps(ledger_fold.candidates(DOC), indent=2) + "\n"
    assert server["call"] == [("demo", None)]


def test_check_without_a_plan_or_with_an_unreadable_one_exits_with_the_reason(server, tmp_path):
    with pytest.raises(SystemExit) as missing:
        ledger_fold.run(args("check"))
    assert missing.value.code == "fold check and fold apply take the plan file: fold check PLAN"
    with pytest.raises(SystemExit) as unreadable:
        ledger_fold.run(args("check", str(tmp_path / "absent.json")))
    assert unreadable.value.code.startswith(f"cannot read the plan {tmp_path / 'absent.json'}: ")


def test_check_prints_ok_for_a_clean_plan_and_the_problems_otherwise(server, tmp_path, capsys):
    ledger_fold.run(args("check", written(tmp_path, plan())))
    assert capsys.readouterr().out == '{"ok": true, "groups": 1}\n'
    with pytest.raises(SystemExit) as refused:
        ledger_fold.run(args("check", written(tmp_path, plan(seams=["Only one"]))))
    assert refused.value.code == 2
    shown = {"problems": ["group 1: seams must list 2 plain sentences, one per folded task"]}
    assert capsys.readouterr().out == json.dumps(shown, indent=2) + "\n"
    assert server["send"] == []


def test_apply_groups_rewrites_the_lead_and_comments_each_member(server, tmp_path, capsys):
    ledger_fold.run(args("apply", written(tmp_path, plan())))
    planned = ledger_fold.writes(DOC, plan()["groups"][0])
    assert server["send"] == [
        ("master@abcdef-0001", "task_group", {"item": "tasks/t1", "members": ["t2"]}),
        ("master@abcdef-0001", "task_update", {"item": "tasks/t1", "fields": planned["lead"]}),
    ]
    slug, ops = server["call"][1]
    entry = dict(ops[0])
    assert slug == "demo" and len(ops) == 1 and re.fullmatch(r"c-[0-9a-f]{10}", entry.pop("id"))
    assert entry == {
        "op": "add",
        "thread": "tasks/t2/comments",
        "text": planned["statuses"]["t2"],
        "by": "master@abcdef-0001",
    }
    assert server["refused"] == [({"rev": 2}, ops)]
    assert capsys.readouterr().out == '{"lead": "t1", "folded": ["t2"]}\n'


def test_apply_stops_before_any_write_when_the_plan_fails_its_check(server, tmp_path):
    with pytest.raises(SystemExit):
        ledger_fold.run(args("apply", written(tmp_path, plan(members=["t9"]))))
    assert server["send"] == [] and server["call"] == [("demo", None)]
