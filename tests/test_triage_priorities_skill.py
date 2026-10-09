import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "profiles/package/skills/triage-priorities/scripts"
SLUG = "demo-2026-01-01"
FAKE = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if args[3:] == ["show"]:
    path = os.path.join(os.environ["LEDGER_DIR"], args[2] + ".json")
    if not os.path.exists(path):
        sys.exit(f"ledger {args[2]} does not exist")
    print(os.environ.get("FAKE_SHOW") or open(path).read())
    sys.exit(0)
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
command = args[5] if len(args) > 5 else ""
if command == os.environ.get("FAKE_FAIL"):
    sys.exit("server refused")
if command == os.environ.get("FAKE_REJECT"):
    print(json.dumps({"posted": False}))
    sys.exit(0)
path = os.path.join(os.environ["LEDGER_DIR"], args[2] + ".json")
doc = json.load(open(path))
if command == "relay" and args[6].startswith("questions/"):
    doc["priorities"] = [p for p in doc["priorities"] if p["item"] != args[6]]
if command == "priority":
    doc["priorities"] = [p for p in doc["priorities"] if p["id"] not in args[7:]]
json.dump(doc, open(path, "w"))
if command == os.environ.get("FAKE_RACED"):
    print(json.dumps({"cleared": args[8:], "rejected": ["op-raced"]}))
    sys.exit(1)
"""


def row(item, by="ledger"):
    return {
        "id": "auto-" + item.replace("/", "-"),
        "item": item,
        "text": "ask about " + item,
        "by": by,
    }


def ledger():
    return {
        "questions": [
            {"id": "q1", "text": "Which broker?", "answers": [], "comments": []},
            {
                "id": "q2",
                "text": "Old?",
                "answers": [{"id": "a", "by": "operator", "text": "yes"}],
                "comments": [],
            },
            {
                "id": "q3",
                "text": "Dropped?",
                "answers": [],
                "comments": [],
                "out_of_scope": True,
            },
        ],
        "tasks": [
            {
                "id": "t1",
                "title": "Ship the queue",
                "state": "pr",
                "awaiting": "approval",
                "comments": [
                    {
                        "id": "c",
                        "by": "eng-1@demo",
                        "text": "checks green, waiting for approval",
                    }
                ],
            },
            {
                "id": "t2",
                "title": "Shipped",
                "state": "done",
                "done": True,
                "comments": [],
            },
            {"id": "t3", "title": "Stuck", "state": "blocked", "comments": []},
        ],
        "followups": [
            {
                "id": "f1",
                "text": "Pick a port",
                "needs_operator": True,
                "done": False,
                "comments": [],
            },
            {"id": "f2", "text": "Closed", "done": True, "comments": []},
        ],
        "phases": [{"id": "p1", "title": "One", "done": False, "comments": []}],
        "priorities": [
            row("questions/q1"),
            row("questions/q2"),
            row("questions/q3"),
            row("tasks/t1"),
            row("tasks/t2"),
            row("tasks/t3"),
            row("followups/f1"),
            row("followups/f2"),
            row("phases/p1", by="eng-2@demo"),
            row("questions/q9"),
        ],
    }


@pytest.fixture
def env(tmp_path):
    bin_dir, ledgers = tmp_path / "bin", tmp_path / "ledgers"
    bin_dir.mkdir()
    ledgers.mkdir()
    fake = bin_dir / "agentihooks"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    (ledgers / f"{SLUG}.json").write_text(json.dumps(ledger()))
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "LEDGER_DIR": str(ledgers),
        "FAKE_LOG": str(tmp_path / "calls.log"),
        "AGENTIHOOKS_AGENT_NAME": "master@a1-1",
    }


def run(env, script, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / script), *args],
        env=env,
        capture_output=True,
        text=True,
    )


def calls(env):
    path = Path(env["FAKE_LOG"])
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def stored(env):
    return json.loads((Path(env["LEDGER_DIR"]) / f"{SLUG}.json").read_text())


def test_list_keeps_items_still_waiting_and_drops_resolved_ones(env):
    result = run(env, "list_priorities.py", SLUG)
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout)
    assert [(p["item"], p["group"]) for p in out["open"]] == [
        ("questions/q1", "questions"),
        ("tasks/t1", "approvals"),
        ("tasks/t3", "blocked"),
        ("followups/f1", "follow ups"),
        ("phases/p1", "phases"),
    ]
    assert {p["item"]: p["why"] for p in out["resolved"]} == {
        "questions/q2": "answered",
        "questions/q3": "out of scope",
        "tasks/t2": "done",
        "followups/f2": "done",
        "questions/q9": "item missing",
    }
    approval = out["open"][1]
    assert approval["item_text"] == "Ship the queue"
    assert approval["recent"] == [{"by": "eng-1@demo", "text": "checks green, waiting for approval"}]
    assert calls(env) == []


def test_list_clears_resolved_priorities_when_asked(env):
    result = run(env, "list_priorities.py", SLUG, "--clear-resolved")
    assert result.returncode == 0, result.stderr
    cleared = calls(env)[0]
    assert cleared[:7] == [
        "ledger",
        "--slug",
        SLUG,
        "--as",
        "master@a1-1",
        "priority",
        "clear",
    ]
    assert sorted(cleared[7:]) == sorted(
        f"auto-{i.replace('/', '-')}"
        for i in (
            "questions/q2",
            "questions/q3",
            "tasks/t2",
            "followups/f2",
            "questions/q9",
        )
    )
    assert [p["item"] for p in stored(env)["priorities"]] == [
        "questions/q1",
        "tasks/t1",
        "tasks/t3",
        "followups/f1",
        "phases/p1",
    ]


def test_list_reports_a_missing_ledger_with_what_the_ledger_command_said(env):
    result = run(env, "list_priorities.py", "nope")
    assert result.returncode == 2
    assert "ledger nope does not exist" in result.stderr
    assert "agentihooks ledger list" in result.stderr


def test_list_refuses_a_ledger_read_that_is_not_json(env):
    result = run({**env, "FAKE_SHOW": "server busy"}, "list_priorities.py", SLUG)
    assert result.returncode == 2
    assert result.stderr.startswith(f"cannot read ledger {SLUG}: ")
    assert "Traceback" not in result.stderr


def write_plan(tmp_path, entries):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(entries))
    return str(path)


def test_apply_runs_each_answer_then_clears_what_is_still_listed(env, tmp_path):
    plan = write_plan(
        tmp_path,
        [
            {
                "priority": "auto-questions-q1",
                "action": "relay",
                "text": "Use the queue.",
                "quote": "the queue",
            },
            {
                "priority": "auto-tasks-t1",
                "action": "relay",
                "text": "Approved, merge it.",
                "quote": "approve",
            },
            {
                "priority": "auto-tasks-t3",
                "action": "task",
                "fields": {"state": "open"},
            },
            {
                "priority": "auto-followups-f1",
                "action": "followup-done",
                "text": "Port 8800 chosen.",
            },
            {"priority": "auto-phases-p1", "action": "keep"},
        ],
    )
    result = run(env, "apply_answers.py", SLUG, plan)
    assert result.returncode == 0, result.stderr
    prefix = ["ledger", "--slug", SLUG, "--as", "master@a1-1"]
    assert calls(env) == [
        [*prefix, "relay", "questions/q1", "Use the queue.", "--quote", "the queue"],
        [*prefix, "relay", "tasks/t1", "Approved, merge it.", "--quote", "approve"],
        [*prefix, "task", "set", "t3", "state=open"],
        [*prefix, "followup", "done", "f1", "--status", "Port 8800 chosen."],
        [
            *prefix,
            "priority",
            "clear",
            "auto-tasks-t1",
            "auto-tasks-t3",
            "auto-followups-f1",
        ],
    ]
    assert [p["item"] for p in stored(env)["priorities"] if p["item"] in ("tasks/t3", "phases/p1")] == ["phases/p1"]
    assert [r["ok"] for r in json.loads(result.stdout)["applied"]] == [
        True,
        True,
        True,
        True,
        True,
    ]


def test_apply_comment_posts_the_masters_own_comment(env, tmp_path):
    plan = write_plan(
        tmp_path,
        [
            {
                "priority": "auto-phases-p1",
                "action": "comment",
                "text": "Planned for later.",
            }
        ],
    )
    assert run(env, "apply_answers.py", SLUG, plan).returncode == 0
    assert calls(env)[0][5:] == ["comment", "phases/p1", "Planned for later."]


def test_apply_refuses_an_invalid_plan_before_running_anything(env, tmp_path):
    plan = write_plan(
        tmp_path,
        [
            {"priority": "auto-questions-q1", "action": "relay", "text": "Yes."},
            {"priority": "auto-phases-p1", "action": "followup-done"},
            {"priority": "nope", "action": "keep"},
            {"priority": "auto-tasks-t1", "action": "launch"},
        ],
    )
    result = run(env, "apply_answers.py", SLUG, plan)
    assert result.returncode == 2
    for needle in ("quote", "followups/", "nope", "launch"):
        assert needle in result.stderr
    assert calls(env) == []


def test_apply_keeps_the_priority_of_an_answer_that_failed(env, tmp_path):
    env = {**env, "FAKE_FAIL": "followup"}
    plan = write_plan(
        tmp_path,
        [
            {"priority": "auto-followups-f1", "action": "followup-done"},
            {
                "priority": "auto-tasks-t3",
                "action": "comment",
                "text": "Unblocked by the new port.",
            },
        ],
    )
    result = run(env, "apply_answers.py", SLUG, plan)
    assert result.returncode == 1
    assert calls(env)[-1][5:] == ["priority", "clear", "auto-tasks-t3"]
    applied = json.loads(result.stdout)["applied"]
    assert [(r["priority"], r["ok"]) for r in applied] == [
        ("auto-followups-f1", False),
        ("auto-tasks-t3", True),
    ]
    assert "server refused" in applied[0]["error"]


def test_list_drops_a_derived_priority_whose_task_moved_on_and_skips_decided_ones(env):
    doc = stored(env)
    doc["tasks"][2]["state"] = "claimed"
    for row in doc["priorities"]:
        row["derived"] = row["by"] == "ledger"
    (Path(env["LEDGER_DIR"]) / f"{SLUG}.json").write_text(json.dumps(doc))
    out = json.loads(run(env, "list_priorities.py", SLUG, "--skip", "auto-questions-q1").stdout)
    assert {"priority": "auto-tasks-t3", "item": "tasks/t3", "why": "no longer waiting"} in out["resolved"]
    assert [p["item"] for p in out["open"]] == ["tasks/t1", "followups/f1", "phases/p1"]
    assert all(len(p["group"]) <= 12 for p in out["open"])


def test_list_reports_a_failed_clear_with_the_ids_and_the_next_step(env):
    result = run({**env, "FAKE_FAIL": "priority"}, "list_priorities.py", SLUG, "--clear-resolved")
    assert result.returncode == 2
    assert "auto-questions-q2" in result.stderr and "server refused" in result.stderr
    assert "Run the same command again" in result.stderr


def test_apply_relays_a_follow_up_decision_before_closing_it(env, tmp_path):
    entry = {"priority": "auto-followups-f1", "action": "followup-done", "text": "Use 8800.", "quote": "8800"}
    assert run(env, "apply_answers.py", SLUG, write_plan(tmp_path, [entry])).returncode == 0
    assert [c[5:] for c in calls(env)] == [
        ["relay", "followups/f1", "Use 8800.", "--quote", "8800"],
        ["followup", "done", "f1"],
        ["priority", "clear", "auto-followups-f1"],
    ]


def test_apply_keeps_the_priority_of_a_comment_the_ledger_refused(env, tmp_path):
    plan = write_plan(tmp_path, [{"priority": "auto-phases-p1", "action": "comment", "text": "Later."}])
    result = run({**env, "FAKE_REJECT": "comment"}, "apply_answers.py", SLUG, plan)
    assert result.returncode == 1
    assert json.loads(result.stdout)["applied"] == [
        {"priority": "auto-phases-p1", "action": "comment", "ok": False, "error": "comment: the ledger refused it"}
    ]
    assert [c[5] for c in calls(env)] == ["comment"]


@pytest.mark.parametrize(
    "plan, needle",
    [
        ({"priority": "auto-phases-p1"}, "must be a list"),
        (["auto-phases-p1"], "is not an object"),
        ([{"priority": {"id": 1}, "action": "keep"}], "is not on the ledger"),
        ([{"priority": "auto-tasks-t3", "action": "task", "fields": "state=open"}], "fields as an object"),
        ([{"priority": "auto-followups-f1", "action": "followup-done", "quote": "8800"}], "needs text and quote"),
        ([{"priority": "auto-phases-p1", "action": "keep"}] * 2, "listed more than once"),
    ],
)
def test_apply_refuses_malformed_plans_with_the_line_to_fix(env, tmp_path, plan, needle):
    result = run(env, "apply_answers.py", SLUG, write_plan(tmp_path, plan))
    assert result.returncode == 2
    assert needle in result.stderr and "Traceback" not in result.stderr
    assert calls(env) == []


def test_apply_counts_a_priority_the_tick_cleared_meanwhile_as_cleared(env, tmp_path):
    plan = write_plan(
        tmp_path,
        [
            {"priority": "auto-tasks-t3", "action": "task", "fields": {"state": "open"}},
            {"priority": "auto-phases-p1", "action": "comment", "text": "Planned for later."},
        ],
    )
    result = run({**env, "FAKE_RACED": "priority"}, "apply_answers.py", SLUG, plan)
    assert result.returncode == 0, result.stdout
    assert [(r["priority"], r["ok"]) for r in json.loads(result.stdout)["applied"]] == [
        ("auto-tasks-t3", True),
        ("auto-phases-p1", True),
    ]


def test_apply_reports_a_clear_that_left_the_priority_listed(env, tmp_path):
    plan = write_plan(tmp_path, [{"priority": "auto-phases-p1", "action": "comment", "text": "Later."}])
    result = run({**env, "FAKE_FAIL": "priority"}, "apply_answers.py", SLUG, plan)
    assert result.returncode == 1
    assert json.loads(result.stdout)["applied"] == [
        {
            "priority": "auto-phases-p1",
            "action": "comment",
            "ok": False,
            "error": "applied, but clearing its priority failed: server refused",
        }
    ]


REFUSED_ROUND = (
    "   Never write the prompts as chat text. When `AskUserQuestion` is refused because the operator is not present, "
    "say only one short line, type operator on to answer the questions here, or leave them in Priorities; "
    "keep every priority and end the run. Done when that one line is sent and every priority is still listed."
)


def test_a_refused_round_sends_one_operator_on_line_and_keeps_every_priority():
    body = (SCRIPTS.parent / "SKILL.md").read_text()

    assert [line for line in body.splitlines() if "type operator on" in line] == [REFUSED_ROUND]
