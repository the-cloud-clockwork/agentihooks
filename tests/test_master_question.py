import json
import os
from pathlib import Path

import pytest

from hooks import hook_manager
from hooks.context import master_question, operator_mode
from scripts.swarm.health.findings import Finding, master_blocked_on_prompt

pytestmark = pytest.mark.unit

SID = "master-session"
NAME = "master@a1b2c3-0001"
NOW = 1_000_000.0
FOCUS = {
    "question": "Which phase should the swarm focus on next?",
    "header": "Focus",
    "options": [
        {"label": "Proof and cleanup", "description": "Finish the chaos test first"},
        {"label": "Hive placement", "description": "Spread engineers across hives"},
    ],
    "multiSelect": False,
}
FREEZE = {"question": "Freeze the hive phase?", "header": "Freeze", "options": [{"label": "Yes"}, {"label": "No"}]}
ROLE_RULE = Path(__file__).resolve().parents[1] / "profiles/package/roles/master/.claude/rules/master-role.md"


class Ledger:
    def __init__(self):
        self.questions = []

    def question(self, slug, text, by, op_id):
        self.questions.append((slug, text, by, op_id))


@pytest.fixture
def master(tmp_path, monkeypatch):
    for key, value in {
        "AGENTIHOOKS_SWARM": "sw",
        "AGENTIHOOKS_SWARM_TASK": "master",
        "AGENTIHOOKS_AGENT_NAME": NAME,
        "AGENTIHOOKS_HOME": str(tmp_path / "ah"),
        "LEDGER_DIR": str(tmp_path / "ledgers"),
    }.items():
        monkeypatch.setenv(key, value)
    return Ledger()


def ask(*questions, tool="AskUserQuestion"):
    tool_input = {"questions": list(questions)}
    transcript = Path(os.environ["AGENTIHOOKS_HOME"]) / "master.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": SID,
                "isSidechain": False,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "ask-1", "name": tool, "input": tool_input}],
                },
            }
        )
        + "\n"
    )
    return {
        "session_id": SID,
        "tool_name": tool,
        "tool_input": tool_input,
        "tool_use_id": "ask-1",
        "transcript_path": str(transcript),
    }


def typed_at(at):
    operator_mode._save(SID, {"on": True, "turn_at": at})


@pytest.mark.parametrize(
    "question",
    [
        {
            "question": "What would you type next?",
            "options": [{"label": "Status of the hub fix?"}, {"label": "Raise the node cap"}],
        },
        {
            "question": "Want the batch runner after-hour count when it ends?",
            "options": [{"label": "Run batch after count"}, {"label": "Check hub fix status"}],
        },
    ],
)
def test_unattributed_suggestions_are_denied_without_ledger_writes(master, monkeypatch, question):
    from scripts.swarm import ledger_client

    payload = ask(question)
    Path(payload["transcript_path"]).write_text("")
    monkeypatch.setattr(ledger_client, "LedgerClient", lambda: master)
    assert hook_manager._operator_question(payload) == operator_mode.question_block("AskUserQuestion", SID)
    assert master.questions == []


@pytest.mark.parametrize("missing", ["tool_use_id", "transcript_path"])
def test_missing_question_provenance_never_writes(master, missing):
    payload = ask(FOCUS)
    payload.pop(missing)
    assert master_question.move(payload, master, now=NOW) == ""
    assert master.questions == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("type", "user"),
        ("sessionId", "other-session"),
        ("isSidechain", True),
        ("isSidechain", None),
        ("message", {"role": "user"}),
    ],
)
def test_only_main_session_assistant_records_authorize_question_writes(master, field, value):
    payload = ask(FOCUS)
    path = Path(payload["transcript_path"])
    record = json.loads(path.read_text())
    record[field] = value
    path.write_text(json.dumps(record) + "\n")
    assert master_question.move(payload, master, now=NOW) == ""
    assert master.questions == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("type", "text"),
        ("id", "other-call"),
        ("name", "Bash"),
        ("input", {"questions": [FREEZE]}),
    ],
)
def test_only_the_exact_recorded_question_call_authorizes_writes(master, field, value):
    payload = ask(FOCUS)
    path = Path(payload["transcript_path"])
    record = json.loads(path.read_text())
    record["message"]["content"][0][field] = value
    path.write_text(json.dumps(record) + "\n")
    assert master_question.move(payload, master, now=NOW) == ""
    assert master.questions == []


@pytest.mark.parametrize("content", ["", "not json\n", "null\n", "{}\n", "[]\n"])
def test_unusable_transcript_records_never_authorize_writes(master, content):
    payload = ask(FOCUS)
    Path(payload["transcript_path"]).write_text(content)
    assert master_question.move(payload, master, now=NOW) == ""
    assert master.questions == []


def test_unreadable_transcript_never_authorizes_writes(master):
    payload = ask(FOCUS)
    Path(payload["transcript_path"]).unlink()
    assert master_question.move(payload, master, now=NOW) == ""
    assert master.questions == []


def test_unrelated_records_do_not_hide_a_deliberate_question_call(master):
    payload = ask(FOCUS)
    path = Path(payload["transcript_path"])
    path.write_text("null\n{}\n" + path.read_text())
    assert master_question.move(payload, master, now=NOW) == master_question.MOVED.format(asked=FOCUS["question"])
    assert len(master.questions) == 1


def test_explicit_priority_commands_pass_the_question_hook_without_automatic_questions(master, monkeypatch):
    from scripts.swarm import ledger_client

    monkeypatch.setattr(ledger_client, "LedgerClient", lambda: master)
    payload = {
        "session_id": SID,
        "tool_name": "Bash",
        "tool_input": {"command": 'agentihooks ledger --slug sw priority add tasks/t1 "Choose the next phase"'},
    }
    assert hook_manager._operator_question(payload) == ""
    assert master.questions == []


def test_a_master_whose_operator_typed_in_the_last_ten_minutes_may_ask(master):
    typed_at(NOW - master_question.WINDOW_SEC + 1)
    assert master_question.move(ask(FOCUS), master, now=NOW) == ""
    assert master.questions == []


def test_a_master_whose_operator_left_ten_minutes_ago_moves_every_question_to_priorities(master):
    typed_at(NOW - master_question.WINDOW_SEC)
    denial = master_question.move(ask(FOCUS, FREEZE), master, now=NOW)
    assert denial == master_question.MOVED.format(asked=f"{FOCUS['question']} {FREEZE['question']}")
    assert [(slug, text, by) for slug, text, by, _ in master.questions] == [
        ("sw", "Which phase should the swarm focus on next? Options: Proof and cleanup; Hive placement.", NAME),
        ("sw", "Freeze the hive phase? Options: Yes; No.", NAME),
    ]


def _gate_rows():
    from scripts.gates import log

    return log.recent("sw", None)


def _row(kind, reason):
    return {
        "at": int(NOW * 1000),
        "gate": "question",
        "kind": kind,
        "agent": NAME,
        "task": "master",
        "tool": "AskUserQuestion",
        "reason": reason,
    }


def test_an_open_question_tool_writes_the_last_typed_turn_to_the_gate_log(master):
    typed_at(NOW - master_question.WINDOW_SEC + 1)
    master_question.move(ask(FOCUS), master, now=NOW)
    assert _gate_rows() == [_row("count", "opened: the operator typed at 1970-01-12T13:36:41Z, 599 seconds ago")]


def test_moved_questions_write_a_deny_row_to_the_gate_log(master):
    typed_at(NOW - master_question.WINDOW_SEC)
    master_question.move(ask(FOCUS, FREEZE), master, now=NOW)
    assert _gate_rows() == [
        _row("deny", "moved 2 questions: the operator typed at 1970-01-12T13:36:40Z, 600 seconds ago")
    ]


def test_a_question_left_to_the_presence_check_writes_that_no_turn_was_read(master):
    payload = ask(FOCUS)
    payload.pop("tool_use_id")
    master_question.move(payload, master, now=NOW)
    assert _gate_rows() == [_row("count", "left to the operator presence check: typed never since his last turn ended")]


def test_a_question_left_to_the_presence_check_writes_the_turn_it_read(master):
    typed_at(NOW - master_question.WINDOW_SEC)
    payload = ask(FOCUS)
    payload.pop("tool_use_id")
    master_question.move(payload, master, now=NOW)
    assert _gate_rows() == [
        _row("count", "left to the operator presence check: typed at 1970-01-12T13:36:40Z, 600 seconds ago")
    ]


def test_a_session_bound_to_no_swarm_writes_no_gate_row(master, monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_SWARM")
    master_coding_seat = Path(os.environ["AGENTIHOOKS_HOME"]) / "master_seats" / f"{SID}.json"
    master_coding_seat.parent.mkdir(parents=True, exist_ok=True)
    master_coding_seat.write_text(json.dumps({"name": NAME}))
    typed_at(NOW - 1)
    assert master_question.move(ask(FOCUS), master, now=NOW) == ""
    from scripts.gates import log

    assert _gate_rows() == [] and log.recent("<slug>", None) == []
    assert not (Path.home() / ".agentihooks" / "swarm" / "_slug_").exists()


def test_a_master_with_no_typed_prompt_moves_the_question(master):
    operator_mode._save(SID, {"on": True, "turn_at": None})
    assert master_question.move(ask(FOCUS), master, now=NOW)
    assert len(master.questions) == 1


def test_a_repeated_question_reuses_its_ledger_operation_id(master):
    master_question.move(ask(FOCUS), master, now=NOW)
    master_question.move(ask(FOCUS), master, now=NOW + 60)
    first, second = master.questions
    assert first[3] == second[3] and len(first[3]) == 32


def test_a_question_asked_again_after_the_operator_returned_gets_a_new_operation_id(master):
    typed_at(NOW - 2 * master_question.WINDOW_SEC)
    master_question.move(ask(FOCUS), master, now=NOW)
    typed_at(NOW)
    master_question.move(ask(FOCUS), master, now=NOW + master_question.WINDOW_SEC)
    first, second = master.questions
    assert first[3] != second[3]


def test_operator_on_with_no_typing_for_ten_minutes_still_moves_the_question(master):
    typed_at(NOW - master_question.WINDOW_SEC)
    assert operator_mode._heard(SID)
    assert master_question.move(ask(FOCUS), master, now=NOW)
    assert len(master.questions) == 1


@pytest.mark.parametrize("task", ["master-2", "master-12"])
def test_a_numbered_master_seat_moves_the_question_too(master, monkeypatch, task):
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", task)
    typed_at(NOW - master_question.WINDOW_SEC)
    assert operator_mode._heard(SID)
    assert master_question.move(ask(FOCUS), master, now=NOW)
    assert len(master.questions) == 1


@pytest.mark.parametrize("task", ["t12", "master-x", "masters", "remaster-2"])
def test_a_task_that_only_resembles_a_master_seat_asks_freely(master, monkeypatch, task):
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", task)
    typed_at(NOW - master_question.WINDOW_SEC)
    assert master_question.move(ask(FOCUS), master, now=NOW) == ""


def test_a_question_without_options_is_moved_as_asked(master):
    master_question.move(ask({"question": "Ship it?"}), master, now=NOW)
    assert master.questions[0][1] == "Ship it?"


def test_other_tools_and_empty_asks_pass(master):
    assert master_question.move(ask(FOCUS, tool="Bash"), master, now=NOW) == ""
    assert master_question.move(ask(), master, now=NOW) == ""
    assert master.questions == []


def test_a_worker_question_is_left_to_the_operator_gate(master, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "t1")
    assert master_question.move(ask(FOCUS), master, now=NOW) == ""
    assert master.questions == []


def test_a_seated_master_without_swarm_env_moves_under_its_ledger_binding(master, monkeypatch, tmp_path):
    import json

    from hooks.context import master_coding

    for key in ("AGENTIHOOKS_SWARM", "AGENTIHOOKS_SWARM_TASK", "AGENTIHOOKS_AGENT_NAME"):
        monkeypatch.delenv(key)
    master_coding.record_seat(SID, "rig", "master@rig-1")
    sessions = tmp_path / "ledgers" / ".sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{SID}.json").write_text(json.dumps({"slug": "rig", "name": "master@rig-1"}))
    assert master_question.move(ask(FREEZE), master, now=NOW)
    assert [(slug, by) for slug, _, by, _ in master.questions] == [("rig", "master@rig-1")]


def test_the_ledger_client_is_built_when_none_is_given(master, monkeypatch):
    from scripts.swarm import ledger_client

    monkeypatch.setattr(ledger_client, "LedgerClient", lambda: master)
    assert master_question.move(ask(FREEZE), now=NOW)
    assert len(master.questions) == 1


def test_the_pre_tool_gate_denies_with_the_moved_notice(master, monkeypatch):
    calls = []
    monkeypatch.setattr(master_question, "move", lambda payload: calls.append(payload) or "moved")
    assert hook_manager._operator_question(ask(FOCUS)) == "moved"
    assert calls == [ask(FOCUS)]


def test_the_pre_tool_gate_falls_back_to_the_operator_gate(master, monkeypatch):
    monkeypatch.setattr(master_question, "move", lambda payload: "")
    assert hook_manager._operator_question(ask(FOCUS)) == operator_mode.question_block("AskUserQuestion", SID)


def test_the_ledger_client_adds_a_question_under_a_stable_operation_id(monkeypatch):
    from scripts.swarm import ledger_client

    sent = []
    monkeypatch.setattr(ledger_client.LedgerClient, "_call", lambda self, slug, ops: sent.append((slug, ops)))
    ledger_client.LedgerClient().question("sw", "Ship it? Options: Yes; No.", NAME, "prompt-1")
    [(slug, [op])] = sent
    assert (slug, op["op"], op["id"], op["by"], op["list"], op["text"]) == (
        "sw",
        "add_item",
        "prompt-1",
        NAME,
        "questions",
        "Ship it? Options: Yes; No.",
    )


def test_a_master_blocked_on_a_prompt_raises_a_finding():
    agents = [
        {"name": "engineer@a1b2c3-0003", "lane": "eng", "input_prompt": FOCUS["question"], "input_ticks": 9},
        {"name": "master@a1b2c3-0002", "lane": "master", "input_prompt": FOCUS["question"], "input_ticks": 3},
        {"name": "master@a1b2c3-0004", "lane": "master", "input_prompt": "", "input_ticks": 9},
        {"name": NAME, "lane": "master", "seat": "master@sw", "input_prompt": FOCUS["question"], "input_ticks": 4},
        {"name": "master@a1b2c3-0005", "lane": "master", "input_prompt": "Ship it?", "input_ticks": 5},
    ]
    assert master_blocked_on_prompt(agents) == [
        Finding(
            "master blocked on prompt",
            "master@sw",
            "the master pane has waited on a question prompt for 4 ticks",
            (f"prompt: {FOCUS['question']}", f"agent {NAME}"),
            "more than 3 consecutive ticks on a question prompt",
            4,
        ),
        Finding(
            "master blocked on prompt",
            "master@a1b2c3-0005",
            "the master pane has waited on a question prompt for 5 ticks",
            ("prompt: Ship it?", "agent master@a1b2c3-0005"),
            "more than 3 consecutive ticks on a question prompt",
            5,
        ),
    ]


def test_the_master_role_rule_keeps_approved_decisions_off_the_question_tool():
    rule = " ".join(ROLE_RULE.read_text().split())
    assert (
        "The question tool is only for a decision you cannot make yourself, never for focus, freeze, rank, scope "
        "or caps the operator already approved." in rule
    )
