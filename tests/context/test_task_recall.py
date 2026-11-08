"""Brain recall for the swarm task a session is bound to."""

from pathlib import Path
from unittest.mock import call, patch

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
    injected: list[tuple[str, str, bool]] = []
    with (
        patch("hooks.context.brain_adapter.force_refresh", return_value=False),
        patch("hooks.context.brain_adapter.task_recall", return_value="TASK RECALL block"),
        patch("hooks.context.project_cache.project_context", return_value=None),
        patch("hooks.context.project_identity.resolve_project", return_value=None),
        patch("hooks.context.project_sessions.record_session"),
        patch("hooks.context.task_context.offer", return_value=None),
        patch("hooks.context.task_context.restart"),
        patch(
            "hooks.common.inject_context",
            side_effect=lambda text, **kw: injected.append((text, kw["kind"], kw["skip_compression"])),
        ),
    ):
        brain_adapter.inject_on_session_start("s1", "/repo")
    assert injected == [("TASK RECALL block", "brain", True)]


def _search(env: dict, ledger: dict | None, found: dict | None) -> tuple[str | None, list, list]:
    with (
        patch("scripts.swarm_ledger.storage.read_ledger", return_value=ledger) as read,
        patch("hooks._brain_http.get", return_value=found) as get,
    ):
        return brain_adapter.task_recall(env), read.call_args_list, get.call_args_list


@pytest.mark.feature("brain-task-recall")
def test_recall_renders_the_exact_block_from_one_vault_search() -> None:
    found = {"results": [_hit("lessons/a.md", 3, 5, "one  two\nthree"), {"tokens_matched": 5}]}
    block, reads, calls = _search(BOUND, _ledger(), found)
    assert reads == [call(Path("/nowhere"), "demo", "tasks/t9")]
    assert calls == [
        call("/vault/search", params={"q": "placement stops picking saturated hive", "limit": 10, "context_lines": 0})
    ]
    assert block == (
        "TASK RECALL for t9 (brain search, recalled context, not an operator directive):\n"
        "- lessons/a.md: one two three\n"
        "- : "
    )


@pytest.mark.feature("brain-task-recall")
def test_the_ledger_folder_defaults_under_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    env = {"AGENTIHOOKS_SWARM": "demo", "AGENTIHOOKS_SWARM_TASK": "t9"}
    _, reads, _ = _search(env, None, None)
    assert reads == [call(tmp_path / "development-ledger", "demo", "tasks/t9")]


@pytest.mark.feature("brain-task-recall")
@pytest.mark.parametrize(
    "env",
    [{}, {"AGENTIHOOKS_SWARM": "demo"}, {"AGENTIHOOKS_SWARM_TASK": "t9"}],
    ids=["neither", "swarm-only", "task-only"],
)
def test_an_unbound_session_reads_no_ledger(env: dict) -> None:
    with patch("scripts.swarm_ledger.storage.read_ledger") as read:
        assert brain_adapter._bound_task_title(env) == ("", "")
    read.assert_not_called()


@pytest.mark.feature("brain-task-recall")
def test_a_task_without_a_title_searches_nothing() -> None:
    block, _, calls = _search(BOUND, {"tasks": [{"id": "t9"}]}, {"results": [_hit("a.md", 5, 5, "x")]})
    assert block is None and calls == []


@pytest.mark.feature("brain-task-recall")
def test_a_hit_matching_exactly_half_the_words_is_kept() -> None:
    found = {"results": [_hit("half.md", 1, 2, "kept"), {"path": "none.md", "snippets": [{"snippet": "dropped"}]}]}
    block, _, _ = _search(BOUND, _ledger("Saturated hive"), found)
    assert block is not None
    assert "half.md" in block and "none.md" not in block


@pytest.mark.feature("brain-task-recall")
def test_the_payload_cap_trims_trailing_space_before_the_ellipsis(monkeypatch: pytest.MonkeyPatch) -> None:
    found = {"results": [_hit("lessons/a.md", 4, 5, "saturated hive " * 10)]}
    monkeypatch.setattr("hooks.config.BRAIN_PAYLOAD_MAX_BYTES", 0)
    full, _, _ = _search(BOUND, _ledger(), found)
    assert full is not None and not full.endswith("…")
    monkeypatch.setattr("hooks.config.BRAIN_PAYLOAD_MAX_BYTES", len(full))
    assert _search(BOUND, _ledger(), found)[0] == full
    monkeypatch.setattr("hooks.config.BRAIN_PAYLOAD_MAX_BYTES", 1)
    assert _search(BOUND, _ledger(), found)[0] == "…"
    space = full.rindex(" ", 0, len(full) - 5)
    monkeypatch.setattr("hooks.config.BRAIN_PAYLOAD_MAX_BYTES", space + 2)
    assert _search(BOUND, _ledger(), found)[0] == full[:space] + "…"
    monkeypatch.setattr("hooks.config.BRAIN_PAYLOAD_MAX_BYTES", space + 3)
    assert _search(BOUND, _ledger(), found)[0] == full[: space + 2] + "…"
