"""An expert works one mission and takes no part in the ledger's planning: it adds no task and no follow up, changes
no task and neither groups nor splits tasks."""

from typing import Any

import pytest

from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger import ledger_groups, ledger_splits, ledger_tasks, new_ledger
from tests.swarm_ledger import legacy_page

pytestmark = pytest.mark.unit

SLUG = "expertrefusals-2026-01-01"
EXPERT = "expert@abcdef-0001"
MASTER = "master@abcdef-0001"
ENGINEER = "engineer@abcdef-0002"
INSIDE = (
    "an expert works one mission to its end: do the work inside the mission or ask the master with agentihooks msg send"
)


@pytest.fixture(autouse=True)
def ledger_dir(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    for name, module in (
        ("task_add", ledger_tasks),
        ("task_update", ledger_tasks),
        ("task_group", ledger_groups),
        ("task_ungroup", ledger_groups),
        ("task_split", ledger_splits),
    ):
        monkeypatch.setitem(core.EXTENSION_OPS, name, module)
    content = {"title": "Demo", "overview": "o", "sources": [], "phases": [{"title": "one", "description": "d"}]}
    html_path, _ = legacy_page.paths(SLUG, core)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    seeded = [
        {"op": "task_add", "id": f"seed-{n}", "by": "swarm", "task": f"t{n}", "title": f"task {n}", "lane": "eng"}
        for n in (1, 2, 3)
    ]
    sized = [
        {"op": "task_update", "id": f"size-{n}", "by": MASTER, "item": f"tasks/t{n}", "fields": {"difficulty": "S"}}
        for n in (1, 2, 3)
    ]
    core.sync(SLUG, ops=[*seeded, *sized])


def sent(op: dict[str, Any]) -> tuple[dict[str, Any], list[str], str]:
    """The ledger after the op, the ops it rejected and the last warning it raised."""
    state, rejected = core.sync(SLUG, ops=[op])
    warnings = state["_meta"]["warnings"]
    return state, rejected, warnings[-1] if warnings else ""


def rows(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {task["id"]: task for task in state["tasks"]}


def added(by: str) -> dict[str, Any]:
    return {"op": "task_add", "id": "add-t9", "by": by, "task": "t9", "title": "x", "lane": "eng", "phase": "p1"}


def followed(by: str) -> dict[str, Any]:
    return {"op": "add_item", "id": "f9", "by": by, "list": "followups", "text": "Check the disk before the next run"}


def changed(by: str, fields: dict[str, Any]) -> dict[str, Any]:
    return {"op": "task_update", "id": "change-t1", "by": by, "item": "tasks/t1", "fields": fields}


def grouped(by: str) -> dict[str, Any]:
    return {"op": "task_group", "id": "group-1", "by": by, "item": "tasks/t1", "members": ["t2"]}


@pytest.mark.feature("expert-claims-nothing")
def test_an_expert_adds_no_task() -> None:
    state, rejected, warning = sent(added(EXPERT))
    assert rejected == ["add-t9"] and "t9" not in rows(state)
    assert warning == f"{EXPERT} cannot add tasks: {INSIDE}"
    state, rejected, _ = sent(added(MASTER))
    assert rejected == [] and "t9" in rows(state)


@pytest.mark.feature("expert-claims-nothing")
def test_an_expert_files_no_follow_up() -> None:
    state, rejected, warning = sent(followed(EXPERT))
    assert rejected == ["f9"] and state.get("followups", []) == []
    assert warning == f"{EXPERT} files no follow up: {INSIDE}"
    state, rejected, _ = sent(followed(ENGINEER))
    assert rejected == [] and [item["id"] for item in state["followups"]] == ["f9"]


def test_an_expert_still_asks_a_question_on_the_ledger() -> None:
    op = {"op": "add_item", "id": "q9", "by": EXPERT, "list": "questions", "text": "Which batch comes first"}
    state, rejected, _ = sent(op)
    assert rejected == [] and [item["id"] for item in state["questions"]] == ["q9"]


@pytest.mark.feature("expert-claims-nothing")
@pytest.mark.parametrize(
    "fields",
    [
        {"state": "claimed", "claimed_by": EXPERT},
        {"difficulty": "M"},
        {"phase": "p1"},
        {"rank": "high"},
        {"description": "Do it another way"},
    ],
)
def test_an_expert_changes_no_task_and_so_claims_none(fields: dict[str, Any]) -> None:
    before = rows(core.sync(SLUG)[0])["t1"]
    state, rejected, warning = sent(changed(EXPERT, fields))
    assert rejected == ["change-t1"] and rows(state)["t1"] == before
    assert warning == f"{EXPERT} changes no task: {INSIDE}"


def test_a_master_still_changes_a_task() -> None:
    state, rejected, _ = sent(changed(MASTER, {"difficulty": "M"}))
    assert rejected == [] and rows(state)["t1"]["difficulty"] == "M"


@pytest.mark.feature("expert-claims-nothing")
def test_an_expert_neither_groups_nor_releases_nor_splits_tasks() -> None:
    state, rejected, warning = sent(grouped(EXPERT))
    assert rejected == ["group-1"] and "merged_into" not in rows(state)["t2"]
    assert warning == f"{EXPERT} works in the expert lane and cannot set a task group"
    state, rejected, _ = sent(grouped("swarm"))
    assert rejected == [] and rows(state)["t2"]["merged_into"] == "t1"
    state, rejected, warning = sent({"op": "task_ungroup", "id": "ungroup-1", "by": EXPERT, "item": "tasks/t1"})
    assert rejected == ["ungroup-1"] and rows(state)["t2"]["merged_into"] == "t1"
    assert warning == f"{EXPERT} works in the expert lane and cannot release a task group"
    split = {"op": "task_split", "id": "split-1", "by": EXPERT, "item": "tasks/t3", "halves": ["1-2", "3-4"]}
    state, rejected, warning = sent(split)
    assert rejected == ["split-1"] and "split" not in rows(state)["t3"]
    assert warning == f"{EXPERT}: expert lane, no split"
