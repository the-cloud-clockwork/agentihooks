import json
import subprocess
import sys
import time
import unittest
from pathlib import Path

import pytest

from hooks.context import operator_words
from scripts.swarm_ledger import ledger
from scripts.swarm_ledger.api import schemas
from tests.swarm_ledger.test_one_line_ids import FAKE_DOM, function_source

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import ledger_relay  # noqa: E402
import new_ledger  # noqa: E402

SLUG = "relay-2026-01-01"
WORDS = "Use the queue, and approve it"
MESSAGES = {
    "relay needs by": "relay needs by, the relaying agent's name",
    "relay needs item": "relay needs item <list>/<id>",
    "relay needs text": f"relay needs text up to {ledger_relay.MAX_TEXT} characters",
    "relay needs quote": "relay needs quote, the operator's words",
}


SPOKEN = (
    " ".join(f"part {n} — the filter runs first, then the classifier – reads each finding" for n in range(1, 20))
    + " -- put my exact words in an open question"
)
DAY = 24 * 3600


def make_ledger():
    content = {
        "title": "Demo",
        "overview": "o",
        "sources": [str(SCRIPTS)],
        "phases": [{"title": "one", "description": "d"}],
        "questions": [{"text": "which broker?"}],
        "followups": [{"text": "check disk"}],
    }
    html_path, json_path = core.paths(SLUG)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(new_ledger.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    state, _ = core.sync(SLUG)
    core.sync(SLUG, ops=[{"op": "join", "id": "j1", "by": "master@a1-1", "role": "orchestrator"}])
    return state


def relay(n, item, text="Use the queue.", quote="use the queue", by="master@a1-1"):
    return {"op": "relay", "id": f"rl-{n}", "by": by, "item": item, "text": text, "quote": quote}


class Relay(unittest.TestCase):
    def setUp(self):
        operator_words.record("master@a1-1", WORDS)
        state = make_ledger()
        self.phase = f"phases/{state['phases'][0]['id']}"
        self.question = f"questions/{state['questions'][0]['id']}"

    def test_a_relayed_answer_is_the_operators_answer_marked_as_relayed(self):
        state, rejected = core.sync(SLUG, ops=[relay(1, self.question, text="  Use the queue.  ")])
        self.assertEqual(rejected, [])
        answer = state["questions"][0]["answers"][-1]
        marks = {"relayed_by": "master@a1-1", "relayed_from": "master pane", "quote": "Use the queue"}
        self.assertEqual(
            answer, {"id": "rl-1", "by": "operator", "at": answer["at"], "text": "Use the queue.", **marks}
        )
        self.assertTrue(answer["at"])
        event = state["_meta"]["events"][-1]
        self.assertEqual(
            {k: event[k] for k in ("by", "kind", "target", "id", "text", *marks)},
            {"by": "operator", "kind": "answer added", "target": self.question, "id": "rl-1", "text": "Use the queue."}
            | marks,
        )
        self.assertEqual(state["_meta"]["stamps"][f"{self.question}/answers"]["by"], "operator")

    def test_a_relayed_answer_drops_the_questions_derived_priority(self):
        state, _ = core.sync(SLUG)
        self.assertIn(self.question, [p["item"] for p in state["priorities"]])
        state, _ = core.sync(SLUG, ops=[relay(2, self.question)])
        self.assertNotIn(self.question, [p["item"] for p in state["priorities"]])

    def test_a_relay_on_another_item_is_an_operator_comment(self):
        state, rejected = core.sync(SLUG, ops=[relay(3, self.phase, text="Approved, merge it.", quote="approve")])
        self.assertEqual(rejected, [])
        comment = state["phases"][0]["comments"][-1]
        self.assertEqual(
            (comment["by"], comment["text"], comment["relayed_by"]), ("operator", "Approved, merge it.", "master@a1-1")
        )
        self.assertEqual(state["_meta"]["events"][-1]["kind"], "comment added")
        self.assertEqual(state["_meta"]["stamps"][f"{self.phase}/comments"]["by"], "operator")

    def test_a_replayed_relay_is_accepted_once(self):
        core.sync(SLUG, ops=[relay(4, self.question)])
        state, rejected = core.sync(SLUG, ops=[relay(4, self.question), relay(5, self.question, text="Again.")])
        self.assertEqual(rejected, [])
        self.assertEqual([a["id"] for a in state["questions"][0]["answers"]], ["rl-4", "rl-5"])

    def test_an_unknown_item_is_rejected(self):
        _, rejected = core.sync(SLUG, ops=[relay(6, "questions/nope")])
        self.assertEqual(rejected, ["rl-6"])

    def test_only_the_orchestrator_relays_and_only_words_the_operator_said(self):
        core.sync(SLUG, ops=[{"op": "join", "id": "j2", "by": "eng-1@a1-1", "role": "member"}])
        operator_words.record("eng-1@a1-1", "Use the queue")
        operator_words.record("stranger", "Use the queue")
        ops = [
            relay(10, self.question, by="eng-1@a1-1"),
            relay(11, self.question, quote="use redis"),
            relay(12, self.question, by="stranger"),
        ]
        state, rejected = core.sync(SLUG, ops=ops)
        self.assertEqual(rejected, ["rl-10", "rl-11", "rl-12"])
        self.assertEqual(state["questions"][0]["answers"], [])

    def test_a_well_formed_relay_passes_the_check(self):
        core.check_body({"ops": [relay(13, self.question, text="x" * ledger_relay.MAX_TEXT)]})

    def test_malformed_relays_are_refused_with_the_missing_field(self):
        for op, message in (
            (relay(14, self.question, by="operator"), "relay needs by"),
            ({k: v for k, v in relay(15, self.question).items() if k != "by"}, "relay needs by"),
            (relay(16, self.question, by="9bad"), "relay needs by"),
            (relay(17, "chat/x"), "relay needs item"),
            ({k: v for k, v in relay(18, self.question).items() if k != "item"}, "relay needs item"),
            (relay(19, self.question, text="  "), "relay needs text"),
            (relay(20, self.question, text=5), "relay needs text"),
            (relay(21, self.question, text="x" * (ledger_relay.MAX_TEXT + 1)), "relay needs text"),
            (relay(22, self.question, quote=""), "relay needs quote"),
            (relay(24, self.question, quote=5), "relay needs quote"),
            ({k: v for k, v in relay(23, self.question).items() if k != "quote"}, "relay needs quote"),
        ):
            with self.subTest(op=op), self.assertRaises(ValueError) as refused:
                ledger_relay.check(op)
            self.assertEqual(str(refused.exception), MESSAGES[message])

    def test_verified_is_empty_without_recorded_words(self):
        self.assertEqual(ledger_relay.verified("nobody", "use the queue"), "")
        self.assertEqual(ledger_relay.verified("nobody", "xx"), "")
        self.assertEqual(ledger_relay.verified("master@a1-1", "the  QUEUE"), "the queue")


def _cli(monkeypatch, item, quote, by="master@a1-1"):
    def call(slug, ops):
        state, rejected = core.sync(slug, ops=ops)
        return {**state, "rejected": rejected}

    monkeypatch.setattr(ledger, "call", call)
    argv = ["--slug", SLUG, "--as", by, "relay", item, "Use the queue.", "--quote", quote]
    ledger.cmd_relay(ledger.build_parser().parse_args(argv))


def test_cli_relays_words_the_operator_said_in_the_pane(monkeypatch, capsys):
    state = make_ledger()
    question = f"questions/{state['questions'][0]['id']}"
    operator_words.record("master@a1-1", "Use the queue for the broker")
    _cli(monkeypatch, question, "use the queue")
    assert json.loads(capsys.readouterr().out) == {"relayed": True, "item": question}
    state, _ = core.sync(SLUG)
    answer = state["questions"][0]["answers"][-1]
    assert (answer["text"], answer["relayed_by"], answer["quote"]) == (
        "Use the queue.",
        "master@a1-1",
        "Use the queue",
    )


def test_cli_refuses_words_the_operator_never_said(monkeypatch):
    state = make_ledger()
    question = f"questions/{state['questions'][0]['id']}"
    with pytest.raises(SystemExit) as refused:
        _cli(monkeypatch, question, "use the queue")
    assert refused.value.code == REFUSED
    state, _ = core.sync(SLUG)
    assert state["questions"][0]["answers"] == []


REFUSED = "relay refused: the quote is not in an operator prompt or answer any master or planner of this swarm recorded"


def test_a_long_quote_with_dashes_from_a_masters_prompt_history_of_yesterday_is_relayed_unchanged(monkeypatch):
    assert len(SPOKEN.split()) >= 200
    state = make_ledger()
    question = f"questions/{state['questions'][0]['id']}"
    core.sync(SLUG, ops=[{"op": "join", "id": "j3", "by": "master@abc123-0002", "role": "orchestrator"}])
    operator_words.record("master@abc123-0001", SPOKEN, now=time.time() - DAY)
    _cli(monkeypatch, question, SPOKEN, by="master@abc123-0002")
    state, _ = core.sync(SLUG)
    assert state["questions"][0]["answers"][-1]["quote"] == SPOKEN


def test_a_planners_words_count_and_an_engineers_or_another_swarms_do_not():
    operator_words.record("planner@abc123-0005", "slice the filters phase", now=time.time() - 3 * DAY)
    operator_words.record("engineer@abc123-0006", "merge the filters work")
    operator_words.record("master@def456-0001", "drop the filters idea")
    assert ledger_relay.verified("master@abc123-0002", "slice the filters phase") == "slice the filters phase"
    assert ledger_relay.verified("master@abc123-0002", "merge the filters work") == ""
    assert ledger_relay.verified("master@abc123-0002", "drop the filters idea") == ""


def test_cli_refuses_a_quote_in_no_operator_prompt_of_the_swarm(monkeypatch):
    state = make_ledger()
    question = f"questions/{state['questions'][0]['id']}"
    operator_words.record("master@abc123-0001", SPOKEN, now=time.time() - DAY)
    with pytest.raises(SystemExit) as refused:
        _cli(monkeypatch, question, "the operator never said this", by="master@abc123-0002")
    assert refused.value.code == REFUSED
    _, rejected = core.sync(SLUG, ops=[relay(30, question, quote="never said this", by="master@abc123-0002")])
    assert rejected == ["rl-30"]


def test_a_quote_of_any_length_passes_the_check():
    ledger_relay.check(relay(31, "questions/q", quote="word — " * 20000))
    long = {"operation_id": "o1", "ops": [relay(32, "questions/q", quote="word — " * 20000)], "guards": {}}
    assert schemas.check_operations(long, core, ()) == long["ops"]


def test_a_relay_onto_a_note_is_his_comment_carrying_only_the_quoted_words():
    make_ledger()
    state, _ = core.sync(SLUG, ops=[{"op": "add", "thread": "notes", "id": "n1", "text": "Later note"}])
    operator_words.record("master@a1-1", "First line.\nKeep the  Filters idea, and more after it")
    state, rejected = core.sync(SLUG, ops=[relay(33, "notes/n1", quote="keep the filters idea")])
    assert rejected == []
    comment = state["notes"][0]["comments"][-1]
    assert (comment["by"], comment["quote"]) == ("operator", "Keep the  Filters idea")


def test_a_quote_the_span_search_misses_is_refused_not_stored_as_the_whole_prompt():
    operator_words.record("master@a1-1", "İstanbul is the city")
    assert operator_words.matching("master@a1-1", "i̇stanbul", within=None)
    assert ledger_relay.verified("master@a1-1", "i̇stanbul") == ""


def test_cli_relay_needs_item_text_and_quote():
    args = ledger.build_parser().parse_args(
        ["--slug", SLUG, "--as", "m", "relay", "questions/q", "Yes.", "--quote", "y"]
    )
    assert (args.command, args.item, args.text, args.quote) == ("relay", "questions/q", "Yes.", "y")
    with pytest.raises(SystemExit):
        ledger.build_parser().parse_args(["--slug", SLUG, "--as", "m", "relay", "questions/q", "Yes."])


def test_the_page_shows_the_relayed_quote_under_the_entry_as_his_words():
    script = (
        FAKE_DOM
        + function_source("h")
        + function_source("hisWords")
        + f"\nconst e = hisWords({{by: 'operator', relayed_by: 'master@a1-1', quote: {json.dumps(SPOKEN)}}});"
        + "\nprocess.stdout.write(JSON.stringify([e.attrs.class, e.kids.map((c) => c.text), hisWords({by: 'operator'})]));"
    )
    out = json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)
    assert out == ["his-words", ["His words", SPOKEN], None]
    assert "hisWords(entry)" in function_source("entryView")


def test_the_page_marks_a_relayed_entry():
    script = (
        FAKE_DOM
        + function_source("h")
        + function_source("relayMark")
        + '\nconst e = relayMark({by: "operator", relayed_by: "master@a1-1", relayed_from: "master pane"});'
        + "\nprocess.stdout.write(JSON.stringify([e.text, relayMark({by: 'operator'})]));"
    )
    out = json.loads(subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True).stdout)
    assert out == ["relayed from the master pane by master@a1-1", None]
    assert "relayMark(entry)" in function_source("entryView")


if __name__ == "__main__":
    unittest.main()
