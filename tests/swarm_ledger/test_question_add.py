import json

import ledger_core as core

from scripts.swarm_ledger import ledger
from tests.swarm_ledger.test_priorities import SLUG, make_ledger


def test_question_add_puts_the_question_on_the_ledger_as_an_agent_event(monkeypatch, capsys):
    make_ledger()

    def call(slug, ops):
        state, rejected = core.sync(slug, ops=ops)
        return {**state, "rejected": rejected}

    monkeypatch.setattr(ledger, "call", call)
    argv = ["--slug", SLUG, "--as", "eng-1@demo", "question", "add", "Which port should the service use?"]
    ledger.cmd_question(ledger.build_parser().parse_args(argv))
    assert json.loads(capsys.readouterr().out) == {"question": "add"}
    state, _ = core.sync(SLUG)
    question = state["questions"][-1]
    assert (question["text"], question["answers"]) == ("Which port should the service use?", [])
    event = state["_meta"]["events"][-1]
    assert (event["by"], event["kind"], event["target"], event["text"]) == (
        "eng-1@demo",
        "added",
        f"questions/{question['id']}",
        "Which port should the service use?",
    )
