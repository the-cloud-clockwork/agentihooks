import json
import sys
from pathlib import Path

import pytest

from scripts.swarm import prompt
from scripts.swarm.store import ASSIST, DELEGATE, FULL, MANUAL, SwarmError
from scripts.swarm_ledger import ledger, ledger_answer

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

from tests.swarm_ledger import legacy_page  # noqa: E402

SLUG = "answer-2026-01-01"
MASTER = "master@a1-1"
WORKER = "engineer@a1-2"


def make_ledger():
    content = {
        "title": "Demo",
        "overview": "o",
        "sources": [str(SCRIPTS)],
        "phases": [{"title": "one", "description": "d"}],
        "questions": [{"text": "which broker?"}],
    }
    html_path, json_path = core.paths(SLUG)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    core.sync(SLUG)
    state, _ = core.sync(
        SLUG,
        ops=[
            {"op": "join", "id": "j1", "by": MASTER, "role": "orchestrator"},
            {"op": "join", "id": "j2", "by": WORKER, "role": "member"},
        ],
    )
    return f"questions/{state['questions'][0]['id']}"


def answer(n, item, by=MASTER, text="Use the queue."):
    return {"op": "answer", "id": f"an-{n}", "by": by, "item": item, "text": text}


def test_the_master_answers_as_itself_and_the_priority_clears():
    question = make_ledger()
    state, _ = core.sync(SLUG)
    assert question in [p["item"] for p in state["priorities"]]
    state, rejected = core.sync(SLUG, ops=[answer(1, question, text="  Use the queue.  ")])
    assert rejected == []
    entry = state["questions"][0]["answers"][-1]
    assert entry == {"id": "an-1", "by": MASTER, "at": entry["at"], "text": "Use the queue."}
    assert entry["at"]
    event = state["_meta"]["events"][-1]
    assert {k: event[k] for k in ("by", "kind", "target", "id", "text")} == {
        "by": MASTER,
        "kind": "answer added",
        "target": question,
        "id": "an-1",
        "text": "Use the queue.",
    }
    assert state["_meta"]["stamps"][f"{question}/answers"]["by"] == MASTER
    assert question not in [p["item"] for p in state["priorities"]]


def test_a_worker_answer_is_refused_and_the_priority_stays():
    question = make_ledger()
    state, rejected = core.sync(SLUG, ops=[answer(1, question, by=WORKER)])
    assert rejected == ["an-1"]
    assert state["questions"][0]["answers"] == []
    assert question in [p["item"] for p in state["priorities"]]


def test_an_answer_from_outside_the_crew_is_refused():
    question = make_ledger()
    state, rejected = core.sync(SLUG, ops=[answer(1, question, by="stranger@a1-9")])
    assert rejected == ["an-1"]
    assert state["questions"][0]["answers"] == []


def test_a_replayed_answer_is_accepted_once_and_an_unknown_question_is_rejected():
    question = make_ledger()
    core.sync(SLUG, ops=[answer(1, question)])
    state, rejected = core.sync(SLUG, ops=[answer(1, question, text="Something else.")])
    assert rejected == []
    assert [a["text"] for a in state["questions"][0]["answers"]] == ["Use the queue."]
    _, rejected = core.sync(SLUG, ops=[answer(2, "questions/missing")])
    assert rejected == ["an-2"]


@pytest.mark.parametrize(
    ("op", "message"),
    [
        ({"by": None}, "answer needs by, the master's name"),
        ({"by": "operator"}, "answer needs by, the master's name"),
        ({"by": "9bad"}, "answer needs by, the master's name"),
        ({"item": "phases/p1"}, "answer needs item questions/<id>"),
        ({"item": "questions/a/b"}, "answer needs item questions/<id>"),
        ({"text": "  "}, f"answer needs text up to {ledger_answer.MAX_TEXT} characters"),
        ({"text": 7}, f"answer needs text up to {ledger_answer.MAX_TEXT} characters"),
        ({"text": "x" * (ledger_answer.MAX_TEXT + 1)}, f"answer needs text up to {ledger_answer.MAX_TEXT} characters"),
    ],
)
def test_malformed_answers_are_refused(op, message):
    with pytest.raises(ValueError) as refused:
        core.check_op({**answer(1, "questions/q"), **op})
    assert str(refused.value) == message


def test_answer_text_passes_the_plain_words_filter():
    core.check_op(answer(1, "questions/q"))
    core.check_op(answer(1, "questions/q", text="a" * ledger_answer.MAX_TEXT))
    with pytest.raises(ValueError):
        core.check_op(answer(1, "questions/q", text="Merged in 0123abcd4567 at 10:42."))


def _cli(monkeypatch, autonomy, by, item):
    def call(slug, ops):
        state, rejected = core.sync(slug, ops=ops)
        return {**state, "rejected": rejected}

    monkeypatch.setattr(ledger, "call", call)
    monkeypatch.setattr(ledger, "swarm_autonomy", {SLUG: autonomy}.__getitem__)
    argv = ["--slug", SLUG, "--as", by, "answer", item, "Use the queue."]
    ledger.cmd_answer(ledger.build_parser().parse_args(argv))


@pytest.mark.parametrize("autonomy", [DELEGATE, FULL])
def test_cli_master_answers_at_delegate_and_full(monkeypatch, capsys, autonomy):
    question = make_ledger()
    _cli(monkeypatch, autonomy, MASTER, question)
    assert json.loads(capsys.readouterr().out) == {"answered": True, "item": question}
    state, _ = core.sync(SLUG)
    assert [(a["by"], a["text"]) for a in state["questions"][0]["answers"]] == [(MASTER, "Use the queue.")]
    assert question not in [p["item"] for p in state["priorities"]]


@pytest.mark.parametrize("autonomy", [MANUAL, ASSIST, ""])
def test_cli_refuses_below_delegate_autonomy(monkeypatch, autonomy):
    question = make_ledger()
    with pytest.raises(SystemExit) as refused:
        _cli(monkeypatch, autonomy, MASTER, question)
    assert refused.value.code == ledger_answer.refusal(autonomy)
    state, _ = core.sync(SLUG)
    assert state["questions"][0]["answers"] == []


def test_cli_refuses_a_worker(monkeypatch):
    question = make_ledger()
    with pytest.raises(SystemExit) as refused:
        _cli(monkeypatch, FULL, WORKER, question)
    assert refused.value.code == ledger.unexplained({"op": "answer", "item": question})
    state, _ = core.sync(SLUG)
    assert state["questions"][0]["answers"] == []


def test_refusal_names_the_autonomy():
    assert ledger_answer.refusal(DELEGATE) == ""
    assert ledger_answer.refusal(FULL) == ""
    assert ledger_answer.refusal(MANUAL) == (
        "answer refused: at manual autonomy the operator answers questions; raise it with priority add"
    )
    assert ledger_answer.refusal("") == "answer refused: this ledger has no swarm, so the operator answers questions"


class _Store:
    def __init__(self, autonomy=None):
        self.autonomy = autonomy

    def config(self, slug):
        if self.autonomy is None or slug != SLUG:
            raise SwarmError(f"no swarm {slug}")
        return type("Config", (), {"autonomy": self.autonomy})()


def test_swarm_autonomy_reads_the_swarm_config(monkeypatch):
    import scripts.swarm.store as store

    monkeypatch.setattr(store, "connect", lambda: _Store(FULL))
    assert ledger.swarm_autonomy(SLUG) == FULL
    monkeypatch.setattr(store, "connect", lambda: _Store())
    assert ledger.swarm_autonomy(SLUG) == ""


def test_the_master_prompt_names_answer_only_at_delegate_and_full(monkeypatch):
    monkeypatch.setattr(prompt, "summary_lines", lambda slug: [])
    line = (
        f"- Answer an agent's question you can decide with agentihooks ledger --slug demo --as {MASTER} answer "
        'questions/<id> "<answer>": it is recorded as yours and leaves the operator\'s Priorities. Raise the rest '
        "to the operator."
    )
    for autonomy, named in ((MANUAL, False), (ASSIST, False), (DELEGATE, True), (FULL, True)):
        text = prompt.build_master("demo", "/repo", MASTER, {}, autonomy=autonomy)
        assert (line in text) is named
