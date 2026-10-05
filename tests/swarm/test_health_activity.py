import pytest

from scripts.swarm.health import activity

BOUND = {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_AGENT_NAME": "sw-eng-1"}


@pytest.mark.parametrize(
    "tool,tool_input,kind",
    [
        ("Bash", {"command": "agentihooks ledger watch sw --as sw-eng-1"}, "watch"),
        ("Bash", {"command": "gh pr checks 12 --json name,bucket"}, "watch"),
        ("Bash", {"command": "gh run watch 991"}, "watch"),
        ("Bash", {"command": "agentihooks swarm sw status --json"}, "watch"),
        ("Bash", {"command": "sleep 30"}, "watch"),
        ("Monitor", {"command": "tail -f x"}, "watch"),
        ("Edit", {"file_path": "a.py"}, "act"),
        ("Write", {"file_path": "a.py"}, "act"),
        ("mcp__serena__replace_symbol_body", {}, "act"),
        ("Bash", {"command": "git commit -m x && gh pr checks 3"}, "act"),
        ("Bash", {"command": "gh pr merge 12 --squash"}, "act"),
        ("Bash", {"command": "git push -u origin x"}, "act"),
        ("Bash", {"command": "cat a.py"}, ""),
        ("Read", {"file_path": "a.py"}, ""),
    ],
)
def test_classify_separates_watching_from_acting(tool, tool_input, kind):
    assert activity.classify(tool, tool_input) == kind


def test_record_counts_only_swarm_bound_sessions(tmp_path):
    activity.record("Monitor", {}, BOUND, tmp_path)
    activity.record("Bash", {"command": "sleep 5"}, BOUND, tmp_path)
    activity.record("Edit", {}, BOUND, tmp_path)
    activity.record("Read", {}, BOUND, tmp_path)
    activity.record("Monitor", {}, {"AGENTIHOOKS_SWARM": "sw"}, tmp_path)
    activity.record("Monitor", {}, {**BOUND, "AGENTIHOOKS_AGENT_NAME": "sw-eng-2"}, tmp_path)
    activity.record("Monitor", {}, {**BOUND, "AGENTIHOOKS_SWARM": "other"}, tmp_path)
    assert activity.counts("sw", tmp_path) == {"sw-eng-1": {"watch": 2, "act": 1}, "sw-eng-2": {"watch": 1, "act": 0}}


def test_an_unsafe_name_or_a_missing_folder_records_and_counts_nothing(tmp_path):
    activity.record("Monitor", {}, {**BOUND, "AGENTIHOOKS_AGENT_NAME": "../x"}, tmp_path)
    assert activity.counts("sw", tmp_path) == {}
    assert activity.counts("none", tmp_path / "missing") == {}


def test_the_pre_tool_hook_records_a_bound_session(monkeypatch, tmp_path):
    from hooks import hook_manager

    for key, value in BOUND.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AGENTIHOOKS_SWARM_TASK", raising=False)
    monkeypatch.setattr(activity, "default_root", lambda: tmp_path)
    hook_manager.on_pre_tool_use(
        {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "sleep 1"}, "cwd": "/"}
    )
    assert activity.counts("sw", tmp_path) == {"sw-eng-1": {"watch": 1, "act": 0}}
