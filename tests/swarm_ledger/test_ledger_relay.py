import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

SLUG = "relay-2026-01-01"


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
        state = make_ledger()
        self.phase = f"phases/{state['phases'][0]['id']}"
        self.question = f"questions/{state['questions'][0]['id']}"

    def test_a_relayed_answer_is_the_operators_answer_marked_as_relayed(self):
        state, rejected = core.sync(SLUG, ops=[relay(1, self.question)])
        self.assertEqual(rejected, [])
        answer = state["questions"][0]["answers"][-1]
        self.assertEqual(
            {k: answer[k] for k in ("by", "text", "relayed_by", "relayed_from", "quote")},
            {
                "by": "operator",
                "text": "Use the queue.",
                "relayed_by": "master@a1-1",
                "relayed_from": "master pane",
                "quote": "use the queue",
            },
        )
        event = state["_meta"]["events"][-1]
        self.assertEqual((event["by"], event["kind"], event["relayed_by"]), ("operator", "answer added", "master@a1-1"))

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

    def test_an_unknown_item_is_rejected(self):
        _, rejected = core.sync(SLUG, ops=[relay(4, "questions/nope")])
        self.assertEqual(rejected, ["rl-4"])

    def test_malformed_relays_are_refused(self):
        for op in (
            relay(5, self.question, by="operator"),
            {k: v for k, v in relay(6, self.question).items() if k != "by"},
            relay(7, "notes/x"),
            relay(8, self.question, text="  "),
            relay(9, self.question, quote=""),
        ):
            with self.subTest(op=op), self.assertRaises(ValueError):
                core.check_body({"ops": [op]})


def _cli(monkeypatch, tmp_path, item, quote):
    import ledger

    def call(slug, ops):
        state, rejected = core.sync(slug, ops=ops)
        return {**state, "rejected": rejected}

    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "master@a1-1")
    monkeypatch.setattr(ledger, "call", call)
    argv = ["--slug", SLUG, "--as", "master@a1-1", "relay", item, "Use the queue.", "--quote", quote]
    ledger.cmd_relay(ledger.build_parser().parse_args(argv))


def test_cli_relays_words_the_operator_said_in_the_pane(monkeypatch, tmp_path, capsys):
    from hooks.context import operator_words

    state = make_ledger()
    question = f"questions/{state['questions'][0]['id']}"
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path)
    operator_words.record("master@a1-1", "Use the queue for the broker")
    _cli(monkeypatch, tmp_path, question, "use the queue")
    assert '"relayed": true' in capsys.readouterr().out
    state, _ = core.sync(SLUG)
    assert state["questions"][0]["answers"][-1]["relayed_by"] == "master@a1-1"


def test_cli_refuses_words_the_operator_never_said(monkeypatch, tmp_path):
    import pytest

    state = make_ledger()
    question = f"questions/{state['questions'][0]['id']}"
    with pytest.raises(SystemExit, match="operator"):
        _cli(monkeypatch, tmp_path, question, "use the queue")
    state, _ = core.sync(SLUG)
    assert state["questions"][0]["answers"] == []


def test_the_page_marks_a_relayed_entry():
    import json
    import subprocess

    from tests.swarm_ledger.test_one_line_ids import FAKE_DOM, function_source

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
