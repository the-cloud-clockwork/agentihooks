"""A failed Claude tool call reaches the retry breaker through the PostToolUseFailure hook."""

import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import install as fixture_install
import pytest

import hooks.context.retry_breaker as breaker
from hooks import hook_manager
from scripts import install

pytestmark = [pytest.mark.unit, pytest.mark.feature("claude-tool-failure-breaker")]

ROOT = Path(__file__).resolve().parents[1]


def _failure(session_id: str, call: int) -> dict:
    """A Claude PostToolUseFailure payload: an error field and no tool response."""
    return {
        "hook_event_name": "PostToolUseFailure",
        "session_id": session_id,
        "tool_name": "Bash",
        "tool_input": {"command": "npm install"},
        "tool_use_id": f"toolu_{call}",
        "error": "Exit code 1\nnpm ERR! ENOENT not found",
        "is_interrupt": False,
    }


@pytest.fixture
def memory_breaker() -> Iterator[None]:
    """The breaker on its in memory store, emptied around the test."""
    breaker._memory_state.clear()
    with patch.object(breaker, "get_redis", return_value=None):
        yield
    breaker._memory_state.clear()


def test_a_claude_failure_payload_is_counted_by_the_retry_breaker(memory_breaker: None) -> None:
    with patch("scripts.swarm.health.activity.finish") as finish:
        for call in range(3):
            hook_manager.EVENT_HANDLERS["PostToolUseFailure"](_failure("claude-fail", call))

    op_key = breaker._compute_operation_key("Bash", {"command": "npm install"})
    state = breaker._get_state("claude-fail", op_key)
    assert state["count"] == 3
    assert state["last_error_text"] == "Exit code 1\nnpm ERR! ENOENT not found"
    assert [c.kwargs for c in finish.call_args_list] == [{"call_id": f"toolu_{n}"} for n in range(3)]


def test_a_claude_failure_trips_the_breaker_at_its_threshold(memory_breaker: None) -> None:
    with (
        patch("scripts.swarm.health.activity.finish"),
        patch.object(breaker, "_inject_breaker_message") as inject,
    ):
        for call in range(5):
            hook_manager.EVENT_HANDLERS["PostToolUseFailure"](_failure("claude-trip", call))

    assert inject.call_count == 1
    assert inject.call_args.args == ("Bash", {"command": "npm install"}, "Exit code 1\nnpm ERR! ENOENT not found", 5)


def test_a_payload_with_neither_a_response_nor_an_error_is_not_counted(memory_breaker: None) -> None:
    payload = _failure("claude-empty", 0)
    del payload["error"]
    breaker.on_post_tool_result(payload)

    assert breaker._memory_state == {}


def test_a_disabled_breaker_leaves_a_claude_failure_uncounted(
    memory_breaker: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("hooks.config.RETRY_BREAKER_ENABLED", False)
    with patch("scripts.swarm.health.activity.finish") as finish:
        hook_manager.EVENT_HANDLERS["PostToolUseFailure"](_failure("claude-off", 0))

    assert breaker._memory_state == {}
    assert finish.call_count == 1


@pytest.fixture
def claude_installer(_isolate_real_user_paths: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """The canonical installer pointed at the isolated Claude home."""
    for name in ("CLAUDE_HOME", "AGENTIHOOKS_STATE_DIR", "STATE_JSON", "_CLAUDE_JSON", "_BASHRC", "AGENTIHOOKS_ROOT"):
        monkeypatch.setattr(install, name, getattr(fixture_install, name))


def test_the_installed_claude_settings_carry_the_failure_event(claude_installer: None) -> None:
    from scripts.targets.claude_target import ClaudeAdapter

    base = json.loads((ROOT / "profiles" / "_base" / "settings.base.json").read_text())
    path = ClaudeAdapter().write_settings(base)

    hooks = json.loads(path.read_text())["hooks"]
    commands = [h["command"] for g in hooks["PostToolUseFailure"] for h in g["hooks"]]
    assert commands == [h["command"] for g in base["hooks"]["PostToolUseFailure"] for h in g["hooks"]]
