"""The marks an entry relayed for the operator carries, as the ledger stores them: a recorded run of every op that
relays his words or one of his entries keeps the text of each stored entry and event."""

import json
from pathlib import Path
from typing import Any

import pytest

from hooks.context import operator_words
from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger import ledger_tasks, new_ledger
from tests.swarm_ledger import legacy_page

pytestmark = pytest.mark.unit

SLUG = "relay-marks"
MASTER = "master@c0ffee-0001"
SAID = "Build the reviewer seats today before anything else runs"
QUOTE = "build the  reviewer seats TODAY"
RECORDED = Path(__file__).with_name("relay_marks_recorded.json")
VOLATILE = ("at", "rev")


def sent(kind: str, by: str | None, ident: str, **fields: Any) -> dict[str, Any]:
    op: dict[str, Any] = {"op": kind, "id": ident, **fields}
    if by is not None:
        op["by"] = by
    core.check_op(op)
    state, rejected = core.sync(SLUG, ops=[op])
    assert rejected == [], state["_meta"]["warnings"]
    return state


def opened(folder: Path, patch: pytest.MonkeyPatch) -> None:
    """A ledger with one phase, one task and one question, the master joined as its orchestrator and the operator's
    words recorded for him."""
    patch.setattr(core, "LEDGER_DIR", folder)
    for name in ledger_tasks.OPS:
        patch.setitem(core.EXTENSION_OPS, name, ledger_tasks)
    content = {
        "title": "Marks",
        "overview": "Intent",
        "sources": [],
        "phases": [{"title": "Build"}],
        "questions": [{"text": "which broker?"}],
    }
    html, json_path = legacy_page.paths(SLUG, core)
    json_path.unlink(missing_ok=True)
    html.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765))
    core.sync(SLUG)
    sent("task_add", "planner", "a-task", task="t1", title="Build it", lane="eng", phase="p1")
    sent("join", MASTER, "a-join", role="orchestrator")
    operator_words.record(MASTER, SAID)


def blanked(found: Any) -> Any:
    """The entry with its times and revisions blanked, every other key and its place kept."""
    if isinstance(found, dict):
        return {key: 0 if key in VOLATILE else blanked(value) for key, value in found.items()}
    if isinstance(found, list):
        return [blanked(value) for value in found]
    return found


def relay_run() -> list[str]:
    """The stored text of every entry and event the relaying ops leave: a note, a comment on it, an answer, a rank,
    an order by his words, an order by an entry he wrote and an order cleared."""
    question = sent("relay", MASTER, "r-note", item="notes", text="Use the queue.", quote=QUOTE)["questions"][0]["id"]
    sent("relay", MASTER, "r-comment", item="notes/r-note", text="Ship it first.", quote=QUOTE)
    sent("relay", MASTER, "r-answer", item=f"questions/{question}", text="The queue.", quote=QUOTE)
    sent("task_update", MASTER, "r-rank", item="tasks/t1", fields={"rank": "high"}, quote=QUOTE)
    sent("order_set", MASTER, "o-quote", item="phases/p1", quote=QUOTE)
    asked = sent("add", None, "c-ask", thread="chat", text="Build the reviewer seats")["chat"][-1]["id"]
    sent("order_set", MASTER, "o-request", item="tasks/t1", request=asked)
    state = sent("order_clear", MASTER, "o-clear", item="phases/p1", quote=QUOTE)
    kept: list[Any] = [
        next(note for note in state["notes"] if note["id"] == "r-note"),
        state["questions"][0]["answers"][-1],
        next(task for task in state["tasks"] if task["id"] == "t1")["ordered"],
        *(event for event in state["_meta"]["events"] if "relayed_by" in event),
    ]
    return [json.dumps(blanked(entry)) for entry in kept]


@pytest.mark.feature("ledger-relay-marks-one-helper")
def test_a_recorded_run_of_the_relaying_ops_keeps_the_text_of_every_entry_and_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened(tmp_path, monkeypatch)
    found = relay_run()
    assert found == json.loads(RECORDED.read_text()), f"the run now leaves: {json.dumps(found)}"
