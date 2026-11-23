import json

import pytest

import hooks.context.swarm_refocus as refocus
from hooks import hook_manager
from hooks.context import master_coding
from hooks.targets.emitter import flush
from scripts.swarm_ledger import plan_read
from scripts.swarm_ledger.repository.sqlite import DATABASE
from tests.swarm_ledger import legacy_page

OBLIGATIONS = (
    "Master obligations: Troubleshoot with read only diagnostics, plan with the operator, "
    "configure the swarm, the ledger and the operator's environment with him through the agentihooks commands "
    "and tools, handle operator inbox items, keep progress current through agentihooks api and judge it; "
    "never edit code or config files in a repository, commit, merge or claim a task.\n"
    "Every loop run agentihooks api --slug <slug> get focus; clear or fix each focus with nothing claimable.\n"
)

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
    legacy_page.store(folder, "rig", LEDGER)
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


def test_the_block_names_the_plan_read_command_for_a_sliced_task_within_the_cap():
    task = {"id": "s1", "phase": "p1", "title": "Slice", "plan_lines": "4-9", "description": "x" * 3000}
    block = refocus.build_block({**LEDGER, "tasks": [task]}, "s1", 1500)
    assert plan_read.pointer(task) in block.splitlines()
    assert len(block) <= 1500


def test_the_block_of_a_task_without_plan_lines_says_nothing_about_plan_read():
    lines = refocus.build_block(LEDGER, "i1", 1500).splitlines()
    assert lines[-2:] == ["Your task i1: Refocus agents", "Inject a refocus block."]


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


def test_each_tool_call_records_its_call_id_for_the_stall_clock(bound, monkeypatch, capsys):
    from scripts.swarm.health import activity

    seen = []
    monkeypatch.setattr(activity, "record", lambda name, tool_input, call_id: seen.append((name, call_id)))
    for call_id in ("toolu_x", None):
        payload = {"session_id": "s1", "tool_name": "Bash", "tool_input": {}, "cwd": "/", "tool_use_id": call_id}
        hook_manager.on_pre_tool_use(payload)
    flush("PreToolUse")
    capsys.readouterr()
    assert seen == [("Bash", "toolu_x"), ("Bash", "")]


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
    legacy_page.store(bound, "rig", ledger)
    assert "A sharper description." in _pre(capsys)
    assert "A sharper description." not in _pre(capsys)


def test_codex_receives_the_block_through_post_tool_use(bound, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    _compact()
    assert "Inject a refocus block." not in _pre(capsys)
    assert "Inject a refocus block." in _post(capsys)
    assert "Inject a refocus block." not in _post(capsys)


def test_a_missing_ledger_lets_the_tool_call_through_silently(bound, capsys):
    (bound / DATABASE).unlink()
    assert "Inject a refocus block." not in _prompt(capsys)
    assert "Inject a refocus block." not in _pre(capsys)


@pytest.mark.parametrize("target", ["claude", "codex"])
def test_each_full_tool_call_counts_once_toward_the_window(bound, monkeypatch, capsys, target):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", target)
    monkeypatch.setenv("AGENTIHOOKS_REFOCUS_EVERY", "3")
    assert "Inject a refocus block." in _prompt(capsys)
    seen = ["Inject a refocus block." in _pre(capsys) + _post(capsys) for _ in range(6)]
    assert seen == [False, False, True, False, False, True]


def test_codex_receives_the_block_on_the_first_prompt(bound, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    out = _prompt(capsys)
    assert "Inject a refocus block." in json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "Inject a refocus block." not in _prompt(capsys)


@pytest.fixture
def compact_events():
    from pathlib import Path

    return json.loads((Path(__file__).parents[1] / "fixtures" / "codex_compact_events.json").read_text())


def _session_start(payload, capsys):
    hook_manager.on_session_start(payload)
    flush("SessionStart")
    return capsys.readouterr().out


@pytest.mark.parametrize("task_id", ["master", "master-2", "i1"])
def test_codex_compact_start_restores_intent_once(bound, monkeypatch, capsys, compact_events, task_id):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", task_id)
    session_id = compact_events[0]["session_id"]
    refocus.refocus_context(session_id, "prompt")
    hook_manager.on_pre_compact(compact_events[0])
    assert "PostCompact" not in hook_manager.EVENT_HANDLERS
    out = _session_start(compact_events[2], capsys)
    assert "SWARM REFOCUS" in out
    if master_coding.MASTER_TASK.fullmatch(task_id):
        assert "Master obligations:" in out
        assert "Your task master" not in out
    else:
        assert "Inject a refocus block." in out
    assert "SWARM REFOCUS" not in _session_start(compact_events[2], capsys)
    assert refocus.refocus_context(session_id, "tool") == ""
    hook_manager.on_pre_compact(compact_events[0])
    assert "SWARM REFOCUS" in _session_start(compact_events[2], capsys)


@pytest.mark.parametrize("task_id", ["master", "i1"])
def test_codex_ordinary_startup_keeps_refocus_on_first_prompt(bound, monkeypatch, capsys, task_id):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", task_id)
    assert "SWARM REFOCUS" not in _session_start({"session_id": "s1", "source": "startup"}, capsys)
    out = _prompt(capsys)
    assert "SWARM REFOCUS" in out
    assert "SWARM REFOCUS" not in _prompt(capsys)


def test_master_block_carries_current_intent_phases_priorities_and_obligations():
    ledger = {
        "title": "Continuity",
        "overview": "Preserve current mission",
        "phases": [
            {"id": "p1", "title": "Finished", "done": True},
            {"id": "p2", "title": "Active", "description": "Restore intent", "done": False},
            {"id": "p3", "title": "Next", "description": "Keep worker intent"},
        ],
        "priorities": [{"text": "Compaction first"}, {"text": "Worker control"}],
        "tasks": [],
    }
    assert refocus.build_block(ledger, "master", 1500) == (
        "=== SWARM REFOCUS: Continuity ===\n" + OBLIGATIONS + "Plan: Preserve current mission\n"
        "Priorities: Compaction first; Worker control\n"
        "Active phases: Active: Restore intent; Next: Keep worker intent"
    )
    ledger["title"] = "t" * 10000
    ledger["overview"] = "x" * 10000
    ledger["phases"][1]["description"] = "y" * 10000
    ledger["priorities"][0]["text"] = "z" * 10000
    block = refocus.build_block(ledger, "master", 1500)
    assert len(block) == 1500
    assert OBLIGATIONS in block
    assert f"Plan: {'x' * 299}…\n" in block
    assert f"\nPriorities: {'z' * 299}…\n" in block
    assert block.endswith(f"Active phases: Active: {'y' * 58}…")
    assert "=== SWARM REFOCUS: " + "t" * 299 + "… ===" in block
    ledger["title"] = "Continuity"
    ledger["overview"] = "Preserve current mission"
    ledger["phases"] = []
    assert refocus.build_block(ledger, "master", 1500).endswith("Priorities: " + "z" * 299 + "…\nActive phases: ")


def test_master_block_clips_active_phases_to_two_shares_when_the_block_fits():
    ledger = {"title": "T", "overview": "O", "phases": [{"title": "Active", "description": "y" * 10000}]}
    assert refocus.build_block(ledger, "master", 1500) == (
        "=== SWARM REFOCUS: T ===\n" + OBLIGATIONS + f"Plan: O\nPriorities: \nActive phases: Active: {'y' * 591}…"
    )


@pytest.mark.feature("master-handoff-carries-orders")
def test_the_master_block_lists_phases_under_an_operator_order_first_by_id_and_title() -> None:
    from scripts.swarm.health import order_check

    ledger = {
        "title": "T",
        "overview": "O",
        "phases": [
            {"id": "p1", "title": "Active", "description": "Restore intent"},
            {"id": "p2", "title": "Seats", "description": "Long text", "ordered": {"at": 7, "by": "operator"}},
            {"id": "p3", "title": "Shipped", "description": "Old", "done": True, "ordered": {"at": 3}},
            {"id": "p4", "title": "Rooms", "description": "More", "ordered": {"at": 5, "by": "operator"}},
            {"id": "p5", "title": "Odd", "description": "Bad mark", "ordered": "yes"},
            {"id": "p6", "title": "Parked", "description": "Dropped", "out_of_scope": True, "ordered": {"at": 1}},
        ],
    }
    assert refocus.build_block(ledger, "master", 1500) == (
        "=== SWARM REFOCUS: T ===\n" + OBLIGATIONS + "Plan: O\nPriorities: \n"
        "Active phases: operator order p4 Rooms; operator order p2 Seats; Active: Restore intent; Odd: Bad mark; "
        "Parked: Dropped"
    )
    assert [entry["item"] for entry in order_check.read(ledger, "")] == ["phases/p4", "phases/p2"]


@pytest.mark.feature("master-handoff-carries-orders")
def test_the_ordered_phases_lead_the_phases_share_when_it_is_clipped() -> None:
    phases = [
        {"id": "p1", "title": "Active", "description": "y" * 10000},
        {"id": "p2", "title": "Seats", "description": "z" * 10000, "ordered": {"at": 7, "by": "operator"}},
    ]
    lead = "operator order p2 Seats; Active: "
    assert refocus.build_block({"title": "T", "overview": "O", "phases": phases}, "master", 1500) == (
        "=== SWARM REFOCUS: T ===\n"
        + OBLIGATIONS
        + f"Plan: O\nPriorities: \nActive phases: {lead}{'y' * (599 - len(lead))}…"
    )


@pytest.mark.feature("master-handoff-carries-orders")
def test_an_ordered_phase_with_no_id_or_title_keeps_its_place() -> None:
    ledger = {"phases": [{"title": "Later", "description": "After"}, {"ordered": {"at": 1}}]}
    assert refocus.build_block(ledger, "master", 1500).endswith("Active phases: operator order  ; Later: After")


@pytest.mark.feature("master-handoff-carries-orders")
def test_listing_the_ordered_phases_loads_no_claim_gate_code() -> None:
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "import sys\n"
        "from hooks.context import swarm_refocus\n"
        "phases = [{'id': 'p1', 'title': 'Seats', 'ordered': {'at': 1}}]\n"
        "block = swarm_refocus.build_block({'phases': phases}, 'master', 1500)\n"
        "gate = ('scripts.swarm.freeze', 'scripts.swarm.claim_hold', 'scripts.swarm.phase_state')\n"
        "print('operator order p1 Seats' in block, [name for name in gate if name in sys.modules])\n"
    )
    root = Path(refocus.__file__).parents[2]
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=root)
    assert done.stdout.split() == ["True", "[]"]


@pytest.mark.parametrize("ledger", [{}, {"phases": [{}], "priorities": [{}]}])
def test_master_sparse_ledger_keeps_obligations_without_invented_intent(ledger):
    phases = ": " if ledger else ""
    assert refocus.build_block(ledger, "master", 1500) == (
        "=== SWARM REFOCUS:  ===\n" + OBLIGATIONS + f"Plan: \nPriorities: \nActive phases: {phases}"
    )


@pytest.mark.feature("swarm-master-focus-read")
def test_the_master_block_and_skill_require_the_focus_read_every_loop() -> None:
    from pathlib import Path

    read = "agentihooks api --slug <slug> get focus"
    assert read in refocus.build_block({"title": "T"}, "master", 1500).partition("\nPlan: ")[0]
    assert read not in refocus.build_block(LEDGER, "i1", 1500)
    skill = Path(refocus.__file__).parents[2] / "profiles/package/roles/master/.claude/skills/swarm-master/SKILL.md"
    assert f"Every loop, read every focus: `{read}`" in skill.read_text().replace("\n  ", " ")


def test_the_ledger_read_names_intent_and_the_focus_of_the_seat(tmp_path):
    legacy_page.store(tmp_path, "rig", dict(LEDGER, priorities=[{"id": "x", "item": "tasks/i1"}]))
    intent = {"title": LEDGER["title"], "overview": LEDGER["overview"], "phases": LEDGER["phases"]}
    assert refocus._read_ledger(tmp_path, "rig", "i1") == dict(intent, tasks=[LEDGER["tasks"][1]])
    assert refocus._read_ledger(tmp_path, "rig", "master") == dict(intent, priorities=[{"id": "x", "item": "tasks/i1"}])
