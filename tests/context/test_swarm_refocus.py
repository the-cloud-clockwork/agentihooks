import json

import pytest

import hooks.context.swarm_refocus as refocus
from hooks import hook_manager
from hooks.targets.emitter import flush

LEDGER = {
    "title": "Rig grade swarm",
    "overview": "Borrow what OpenRig does that we lack.",
    "phases": [
        {"id": "p1", "title": "Messaging", "description": "One durable inbox."},
        {"id": "p3", "title": "Intent and plan", "description": "Agents get the intent back after compaction."},
    ],
    "tasks": [
        {"id": "m1", "phase": "p1", "title": "Inbox", "description": "Build the inbox."},
        {"id": "i1", "phase": "p3", "title": "Refocus agents", "description": "Inject a refocus block."},
    ],
}


@pytest.fixture
def ledger_dir(tmp_path, monkeypatch):
    folder = tmp_path / "ledgers"
    folder.mkdir()
    (folder / "rig.json").write_text(json.dumps(LEDGER), encoding="utf-8")
    monkeypatch.setenv("LEDGER_DIR", str(folder))
    monkeypatch.setattr(refocus, "STATE_DIR", tmp_path / "refocus-state")
    monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_REFOCUS_EVERY", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_REFOCUS_MAX_CHARS", raising=False)
    return folder


@pytest.fixture
def bound(ledger_dir, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "rig")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "i1")
    return ledger_dir


@pytest.fixture
def unbound(ledger_dir, monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_SWARM_TASK", raising=False)
    return ledger_dir


def _prompt(capsys):
    hook_manager.on_user_prompt_submit({"session_id": "s1", "prompt": "work", "cwd": "/"})
    flush("UserPromptSubmit")
    return capsys.readouterr().out


def _pre(capsys):
    hook_manager.on_pre_tool_use({"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "/"})
    flush("PreToolUse")
    return capsys.readouterr().out


def _post(capsys):
    hook_manager.on_post_tool_use(
        {
            "session_id": "s1",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "tool_response": {"stdout": "a"},
            "cwd": "/",
        }
    )
    flush("PostToolUse")
    return capsys.readouterr().out


def _compact():
    hook_manager.on_pre_compact({"session_id": "s1"})


def test_the_block_carries_overview_phase_and_task_intent():
    block = refocus.build_block(LEDGER, "i1", 1500)
    for text in (
        "Borrow what OpenRig does that we lack.",
        "Intent and plan",
        "Agents get the intent back after compaction.",
        "Refocus agents",
        "Inject a refocus block.",
    ):
        assert text in block
    assert "Build the inbox." not in block


def test_an_unknown_task_builds_nothing():
    assert refocus.build_block(LEDGER, "zz", 1500) == ""


def test_the_block_stays_under_the_cap_with_a_long_description():
    ledger = json.loads(json.dumps(LEDGER))
    ledger["tasks"][1]["description"] = "word " * 2000
    block = refocus.build_block(ledger, "i1", 800)
    assert len(block) <= 800
    assert "Refocus agents" in block and "Intent and plan" in block


def test_a_bound_session_gets_the_block_on_the_first_prompt_only(bound, capsys):
    assert "Inject a refocus block." in _prompt(capsys)
    assert "Inject a refocus block." not in _prompt(capsys)


def test_a_bound_session_gets_the_block_again_after_compaction(bound, capsys):
    assert "Inject a refocus block." in _prompt(capsys)
    assert "Inject a refocus block." not in _pre(capsys)
    _compact()
    assert "Inject a refocus block." in _pre(capsys)
    assert "Inject a refocus block." not in _pre(capsys)


def test_an_unbound_session_gets_nothing(unbound, capsys):
    assert "Inject a refocus block." not in _prompt(capsys)
    _compact()
    assert "Inject a refocus block." not in _pre(capsys)


def test_an_unchanged_block_waits_for_the_window_and_a_changed_one_does_not(bound, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIHOOKS_REFOCUS_EVERY", "3")
    assert "Inject a refocus block." in _prompt(capsys)
    assert "Inject a refocus block." not in _pre(capsys)
    assert "Inject a refocus block." not in _pre(capsys)
    assert "Inject a refocus block." in _pre(capsys)
    ledger = json.loads(json.dumps(LEDGER))
    ledger["tasks"][1]["description"] = "A sharper description."
    (bound / "rig.json").write_text(json.dumps(ledger), encoding="utf-8")
    assert "A sharper description." in _pre(capsys)
    assert "A sharper description." not in _pre(capsys)


def test_codex_receives_the_block_through_post_tool_use(bound, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    _compact()
    assert "Inject a refocus block." not in _pre(capsys)
    assert "Inject a refocus block." in _post(capsys)
    assert "Inject a refocus block." not in _post(capsys)


def test_a_missing_ledger_lets_the_tool_call_through_silently(bound, capsys):
    (bound / "rig.json").unlink()
    assert "Inject a refocus block." not in _prompt(capsys)
    assert "Inject a refocus block." not in _pre(capsys)
