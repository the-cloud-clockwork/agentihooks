"""Bash output filter on Claude, copilot PreToolUse single envelope, enforcement
counter locking/bounding/reset, and repo-scoped MCP enforcement tools."""

import json
import threading
from pathlib import Path

import pytest

import hooks.hook_manager as hm
from hooks.context import enforcement
from hooks.targets import emitter

pytestmark = pytest.mark.unit


def _post_bash(command: str, stdout: str) -> dict:
    return {
        "hook_event_name": "PostToolUse",
        "session_id": "sid-bash-filter",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": {"stdout": stdout, "stderr": "", "interrupted": False, "isImage": False},
        "cwd": "/tmp",
    }


def _single_json(out: str) -> dict:
    out = out.strip()
    assert out.count("\n") == 0 and out.startswith("{"), out
    return json.loads(out)


@pytest.fixture
def forced_claude(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    monkeypatch.setattr(emitter, "_forced", True)
    for flag in ("BRAIN_ENABLED", "BROADCAST_ENABLED", "QUOTA_USAGE_INJECTION_ENABLED"):
        monkeypatch.setattr(f"hooks.config.{flag}", False)


class TestBashFilterOnClaude:
    def test_kubectl_output_is_replaced_not_duplicated(self, forced_claude, capsys):
        stdout = "\n".join(f"pod-{i} Running" for i in range(120))
        hm.on_post_tool_use(_post_bash("kubectl get pods -A", stdout))
        emitter.flush("PostToolUse")
        out = _single_json(capsys.readouterr().out)["hookSpecificOutput"]
        replaced = out["updatedToolOutput"]
        assert replaced["stdout"].startswith("[truncated: kept last 50 of 120 lines]")
        assert replaced["stdout"].endswith("pod-119 Running")
        assert replaced["interrupted"] is False
        assert "pod-119" not in out.get("additionalContext", "")

    def test_generic_output_passes_through(self, forced_claude, capsys):
        hm.on_post_tool_use(_post_bash("cat big.log", "x" * 20_000))
        emitter.flush("PostToolUse")
        assert "updatedToolOutput" not in capsys.readouterr().out

    def test_condition_replacement_wins(self, forced_claude, capsys, monkeypatch):
        from hooks.context import conditions

        monkeypatch.setattr(
            conditions,
            "post_effect",
            lambda payload: conditions.PostEffect(contexts=[], hook_fields={"updatedToolOutput": {"stdout": "mine"}}),
        )
        hm.on_post_tool_use(_post_bash("kubectl get pods -A", "\n".join(str(i) for i in range(120))))
        emitter.flush("PostToolUse")
        assert _single_json(capsys.readouterr().out)["hookSpecificOutput"]["updatedToolOutput"] == {"stdout": "mine"}


class TestCopilotPreToolUseEnvelope:
    def test_blocks_and_buffered_context_share_one_object(self, monkeypatch, capsys):
        monkeypatch.setenv("AGENTIHOOKS_TARGET", "copilot")
        monkeypatch.setattr("hooks.config.BROADCAST_ENABLED", False)
        monkeypatch.setattr("hooks.config.BRAIN_ENABLED", False)
        monkeypatch.setattr("hooks.config.QUOTA_USAGE_INJECTION_ENABLED", False)
        monkeypatch.setattr(enforcement, "get_pretool_enforcements", lambda *a, **k: "ENFORCEMENT-CANARY")
        from hooks.common import inject_context

        inject_context("EARLIER-CANARY", also_log=False)
        hm.on_pre_tool_use(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "sid-copilot-envelope",
                "tool_name": "Read",
                "tool_input": {"file_path": "/tmp/x"},
                "cwd": "/tmp",
            }
        )
        emitter.flush("PreToolUse")
        out = _single_json(capsys.readouterr().out)
        assert set(out) == {"additionalContext"}
        assert "EARLIER-CANARY" in out["additionalContext"]
        assert "ENFORCEMENT-CANARY" in out["additionalContext"]


class TestCounters:
    def test_paths_are_isolated_from_the_real_home(self):
        real = Path("~").expanduser()
        for path in (enforcement._counter_path(), enforcement._delivery_path(), enforcement._store_path()):
            assert str(path).startswith(str(real)) and "/_home/" in str(path)

    def test_concurrent_increments_are_not_lost(self):
        def bump():
            for _ in range(25):
                enforcement.increment_and_get_count("sid-race")

        threads = [threading.Thread(target=bump) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert enforcement._load_counters()["sid-race"] == 200

    def test_file_keeps_the_most_recent_sessions(self, monkeypatch):
        monkeypatch.setattr(enforcement, "_SESSION_KEEP", 3)
        for sid in ("a", "b", "c", "d"):
            enforcement.increment_and_get_count(sid)
        enforcement.increment_and_get_count("b")
        enforcement.increment_and_get_count("e")
        assert list(enforcement._load_counters()) == ["d", "b", "e"]

    def test_session_end_resets_the_counter(self, monkeypatch):
        monkeypatch.setattr("hooks.config.BROADCAST_ENABLED", False)
        enforcement.increment_and_get_count("sid-end")
        hm.on_session_end({"hook_event_name": "SessionEnd", "session_id": "sid-end", "reason": "other"})
        assert "sid-end" not in enforcement._load_counters()


class _CaptureMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def register(func):
            self.tools[func.__name__] = func
            return func

        return register


@pytest.fixture
def mcp_tools():
    from hooks.mcp import enforcement as mcp_enforcement

    capture = _CaptureMCP()
    mcp_enforcement.register(capture)
    return capture.tools


@pytest.fixture
def repo(tmp_path):
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


class TestMCPLocalScope:
    def test_local_set_list_clear_stay_in_the_repo(self, mcp_tools, repo, monkeypatch):
        monkeypatch.chdir(repo)
        created = json.loads(mcp_tools["enforcement_set"]("repo only", 3, local=True))
        assert created["success"] and created["scope"] == "local"
        stored = json.loads((repo / ".agentihooks" / "enforcements.json").read_text())["enforcements"]
        assert [e["message"] for e in stored] == ["repo only"]
        assert not enforcement._store_path().exists()

        listed = json.loads(mcp_tools["enforcement_list"](local=True))
        assert [e["message"] for e in listed["enforcements"]] == ["repo only"]
        assert json.loads(mcp_tools["enforcement_list"]())["count"] == 0

        cleared = json.loads(mcp_tools["enforcement_clear"](enforcement_id=created["enforcement_id"], local=True))
        assert cleared["cleared"] == 1

    def test_explicit_cwd_wins_over_process_cwd(self, mcp_tools, repo, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert json.loads(mcp_tools["enforcement_set"]("x", 2, local=True, cwd=str(repo)))["success"]
        assert (repo / ".agentihooks" / "enforcements.json").exists()

    def test_local_outside_a_repo_fails(self, mcp_tools, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = json.loads(mcp_tools["enforcement_set"]("x", 2, local=True))
        assert result["success"] is False and "Git project" in result["error"]
