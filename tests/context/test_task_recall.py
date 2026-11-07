"""Brain recall for the swarm task a session is bound to."""

from pathlib import Path
from unittest.mock import patch

import pytest

from hooks.context import brain_adapter

TITLE = "Placement stops picking a saturated hive"
BOUND = {"AGENTIHOOKS_SWARM": "demo", "AGENTIHOOKS_SWARM_TASK": "t9", "LEDGER_DIR": "/nowhere"}


def _ledger(title: str = TITLE) -> dict:
    return {"tasks": [{"id": "t9", "title": title}]}


def _hit(path: str, matched: int, total: int, snippet: str) -> dict:
    return {"path": path, "tokens_matched": matched, "tokens_total": total, "snippets": [{"snippet": snippet}]}


def _recall(env: dict, ledger: dict | None, found: dict | None) -> tuple[str | None, list]:
    with (
        patch("scripts.swarm_ledger.storage.read_ledger", return_value=ledger),
        patch("hooks._brain_http.get", return_value=found) as get,
    ):
        return brain_adapter.task_recall(env), get.call_args_list


@pytest.mark.feature("brain-task-recall")
def test_a_bound_task_recalls_brain_hits_for_its_title() -> None:
    found = {"results": [_hit("lessons/hive-cpu.md", 3, 4, "A saturated hive kept winning placement")]}
    block, calls = _recall(BOUND, _ledger(), found)
    assert block is not None
    assert "TASK RECALL" in block and "t9" in block
    assert "recalled context, not an operator directive" in block
    assert "lessons/hive-cpu.md" in block and "A saturated hive kept winning placement" in block
    query = calls[0].kwargs["params"]["q"].split()
    assert query == ["placement", "stops", "picking", "saturated", "hive"]


@pytest.mark.feature("brain-task-recall")
def test_hits_matching_under_half_the_query_words_are_dropped() -> None:
    found = {"results": [_hit("journal/noise.md", 2, 5, "hive"), _hit("lessons/keep.md", 3, 5, "keep me")]}
    block, _ = _recall(BOUND, _ledger(), found)
    assert block is not None
    assert "lessons/keep.md" in block and "journal/noise.md" not in block


@pytest.mark.feature("brain-task-recall")
@pytest.mark.parametrize(
    ("env", "ledger", "found"),
    [
        ({}, _ledger(), {"results": [_hit("a.md", 5, 5, "x")]}),
        (BOUND, {"tasks": []}, {"results": [_hit("a.md", 5, 5, "x")]}),
        (BOUND, None, {"results": [_hit("a.md", 5, 5, "x")]}),
        (BOUND, _ledger(), None),
        (BOUND, _ledger(), {"results": [_hit("a.md", 1, 5, "x")]}),
        (BOUND, _ledger("Fix it"), {"results": [_hit("a.md", 5, 5, "x")]}),
    ],
    ids=["unbound", "task-absent", "ledger-unreadable", "search-unavailable", "weak-hits", "no-significant-words"],
)
def test_no_recall_without_a_bound_task_and_a_matching_hit(env: dict, ledger: dict | None, found: dict | None) -> None:
    block, _ = _recall(env, ledger, found)
    assert block is None


@pytest.mark.feature("brain-task-recall")
def test_recall_is_capped_by_the_payload_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hooks.config.BRAIN_PAYLOAD_MAX_BYTES", 200)
    found = {"results": [_hit(f"lessons/l{i}.md", 4, 5, "saturated hive " * 40) for i in range(5)]}
    block, _ = _recall(BOUND, _ledger(), found)
    assert block is not None
    assert len(block) <= 200


@pytest.mark.feature("brain-task-recall")
def test_session_start_injects_the_task_recall(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("hooks.config.AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr("hooks.config.BRAIN_ENABLED", True)
    injected: list[tuple[str, str]] = []
    with (
        patch("hooks.context.brain_adapter.force_refresh", return_value=False),
        patch("hooks.context.brain_adapter.task_recall", return_value="TASK RECALL block"),
        patch("hooks.context.project_cache.project_context", return_value=None),
        patch("hooks.context.project_identity.resolve_project", return_value=None),
        patch("hooks.context.project_sessions.record_session"),
        patch("hooks.context.task_context.offer", return_value=None),
        patch("hooks.context.task_context.restart"),
        patch("hooks.common.inject_context", side_effect=lambda text, **kw: injected.append((text, kw["kind"]))),
    ):
        brain_adapter.inject_on_session_start("s1", "/repo")
    assert injected == [("TASK RECALL block", "brain")]
