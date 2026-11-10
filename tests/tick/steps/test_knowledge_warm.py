"""The knowledge warm tick step keeps a running swarm's knowledge index level with its ledger export."""

import time
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.ledger_knowledge import service
from scripts.ledger_knowledge.index import HashEmbedder, KnowledgeIndex
from scripts.tick import registry
from scripts.tick.adapters.fakes import FakeLedger, fake_tools
from scripts.tick.deadline import Deadline
from scripts.tick.engine import cadence_s, interval_s
from scripts.tick.steps import CallStep, pass_domains

pytestmark = pytest.mark.unit

SLUG = "sw"
CLI = SimpleNamespace(now_ms=lambda: int(time.time() * 1000))


def _step() -> CallStep:
    import_module("scripts.tick.steps.knowledge")
    found = {step.name: step for step in registry.steps()}["knowledge warm"]
    assert isinstance(found, CallStep)
    return found


def _document(*tasks: str) -> dict[str, Any]:
    return {
        "title": "Ledger",
        "overview": "what the ledger builds",
        "phases": [{"id": "p1", "title": "Phase one"}],
        "tasks": [{"id": task, "title": f"Task {task} about knowledge", "phase": "p1"} for task in tasks],
    }


def _values(document: dict[str, Any]) -> dict[str, Any]:
    tools = fake_tools()
    assert isinstance(tools.ledger, FakeLedger)
    tools.ledger.answers["export"] = document
    return {
        "slug": SLUG,
        "tools": tools.for_effects(frozenset({"ledger"})),
        "deadline": Deadline.after(5),
        "fakes": tools,
    }


def _index(home: Path) -> KnowledgeIndex:
    return KnowledgeIndex(service.index_path(SLUG, {"AGENTIHOOKS_HOME": str(home)}), HashEmbedder())


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.delenv("AGENTIHOOKS_KNOWLEDGE_EMBEDDER", raising=False)
    return tmp_path


@pytest.mark.feature("tick-knowledge-warm")
def test_knowledge_warm() -> None:
    """The knowledge warm step registers in group wrapper metrics on the slow lane with its feature and ports."""
    found = _step()
    assert (found.group, found.lane, found.feature, found.effects) == (
        "wrapper metrics",
        "slow",
        "tick-knowledge-warm",
        frozenset({"ledger"}),
    )
    assert found.reads == pass_domains("slug")


@pytest.mark.feature("tick-knowledge-warm")
def test_the_step_runs_inside_the_window_an_ask_treats_as_fresh() -> None:
    assert interval_s(_step(), cadence_s({})) < service.REFRESH_SECONDS


@pytest.mark.feature("tick-knowledge-warm")
def test_a_task_added_to_the_ledger_is_in_the_index_after_one_run(_home: Path) -> None:
    assert _step().call(CLI, _values(_document("t1")).__getitem__) == [
        f"knowledge warm of {SLUG}: 3 added, 0 changed, 0 removed"
    ]
    assert _step().call(CLI, _values(_document("t1")).__getitem__) == []
    assert _step().call(CLI, _values(_document("t1", "t2")).__getitem__) == [
        f"knowledge warm of {SLUG}: 1 added, 0 changed, 0 removed"
    ]
    hits = _index(_home).query("Task t2 about knowledge", 1)
    assert [(hit.chunk.ref, [parent.ref for parent in hit.parents]) for hit in hits] == [
        ("tasks/t2", ["phases/p1", "ledger"])
    ]


@pytest.mark.feature("tick-knowledge-warm")
def test_the_step_reads_the_ledger_export_under_its_deadline() -> None:
    values = _values(_document("t1"))
    _step().call(CLI, values.__getitem__)
    assert [(call.operation, call.deadline) for call in values["fakes"].ledger.calls] == [
        ("export", values["deadline"])
    ]


@pytest.mark.feature("tick-knowledge-warm")
def test_a_run_while_an_earlier_refresh_still_runs_skips_without_reading_the_ledger(_home: Path) -> None:
    knowledge = import_module("scripts.tick.steps.knowledge")
    path = service.index_path(SLUG, {"AGENTIHOOKS_HOME": str(_home)})
    assert knowledge._claim(path) is True
    try:
        values = _values(_document("t1"))
        assert _step().call(CLI, values.__getitem__) == [
            f"knowledge warm of {SLUG} skipped: an earlier refresh is still running"
        ]
        assert values["fakes"].ledger.calls == []
    finally:
        knowledge._release(path)
    assert _step().call(CLI, _values(_document("t1")).__getitem__) == [
        f"knowledge warm of {SLUG}: 3 added, 0 changed, 0 removed"
    ]


@pytest.mark.feature("tick-knowledge-warm")
def test_a_failed_refresh_releases_the_index_for_the_next_run(_home: Path) -> None:
    values = _values(_document("t1"))
    values["deadline"] = Deadline.after(-1)
    with pytest.raises(TimeoutError, match="tool deadline expired"):
        _step().call(CLI, values.__getitem__)
    assert _step().call(CLI, _values(_document("t1")).__getitem__) == [
        f"knowledge warm of {SLUG}: 3 added, 0 changed, 0 removed"
    ]


@pytest.mark.feature("tick-knowledge-warm")
def test_an_ask_right_after_the_step_ran_answers_without_reading_the_ledger(_home: Path) -> None:
    _step().call(CLI, _values(_document("t1", "t2")).__getitem__)

    def unread() -> dict[str, Any]:
        raise AssertionError("ask refreshed a warm index")

    answer = service.ask(_index(_home), unread, "Task t2 about knowledge", int(time.time() * 1000))
    assert answer["hits"][0]["ref"] == "tasks/t2"
