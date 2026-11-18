"""Each agent of one Claude session carries the Serena root it activated itself, recorded by session and agent id."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

import hooks.context.serena_binding as serena_binding
import hooks.hook_manager as hm
from hooks._redis import redis_key
from hooks.serena_router.binding import ROOT_ARG
from hooks.targets import emitter

if TYPE_CHECKING:
    import fakeredis

pytestmark = pytest.mark.unit

SESSION = "sid-serena-binding"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> dict[str, Path]:
    primary = (tmp_path / "primary").resolve()
    primary.mkdir()
    _git(primary, "init", "-q", "-b", "dev")
    _git(primary, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    for name in ("a", "b"):
        _git(primary, "worktree", "add", "-q", "-b", name, str(tmp_path / f"wt-{name}"))
    return {"primary": primary, "a": (tmp_path / "wt-a").resolve(), "b": (tmp_path / "wt-b").resolve()}


@pytest.fixture
def store() -> Iterator[fakeredis.FakeRedis]:
    import fakeredis

    client = fakeredis.FakeRedis(decode_responses=True)
    with patch("hooks._redis.get_redis", return_value=client):
        yield client


@pytest.fixture
def forced_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    monkeypatch.setattr(emitter, "_forced", True)
    monkeypatch.setattr(emitter, "_buffer", [])


def _payload(
    tool: str, tool_input: dict[str, Any], agent_id: str = "", mode: str = "bypassPermissions"
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "hook_event_name": "PreToolUse",
        "session_id": SESSION,
        "tool_name": f"mcp__serena__{tool}",
        "tool_input": tool_input,
        "permission_mode": mode,
        "cwd": "/tmp",
    }
    if agent_id:
        payload["agent_id"] = agent_id
    return payload


def _activate(project: Path | str, agent_id: str = "") -> dict[str, str] | None:
    return serena_binding.rewrite(_payload("activate_project", {"project": str(project)}, agent_id))


@pytest.mark.feature("serena-hook-carries-each-agent-activated-root")
def test_a_sub_agent_activation_leaves_the_parent_root_unchanged(
    store: fakeredis.FakeRedis, repo: dict[str, Path]
) -> None:
    assert _activate(repo["a"]) is None
    assert _activate(repo["b"], agent_id="sub-1") is None
    assert serena_binding.rewrite(_payload("replace_content", {"relative_path": "f.txt"})) == {ROOT_ARG: str(repo["a"])}
    assert serena_binding.rewrite(_payload("replace_content", {}, agent_id="sub-1")) == {ROOT_ARG: str(repo["b"])}


@pytest.mark.feature("serena-hook-carries-each-agent-activated-root")
def test_a_sub_agent_without_its_own_activation_carries_the_parent_root(
    store: fakeredis.FakeRedis, repo: dict[str, Path]
) -> None:
    _activate(repo["a"])
    _activate(repo["b"], agent_id="sibling")
    assert serena_binding.rewrite(_payload("find_symbol", {}, agent_id="sub-2")) == {ROOT_ARG: str(repo["a"])}


@pytest.mark.feature("serena-hook-carries-each-agent-activated-root")
def test_an_activated_subfolder_records_its_checkout_root(store: fakeredis.FakeRedis, repo: dict[str, Path]) -> None:
    folder = repo["a"] / "pkg"
    folder.mkdir()
    _activate(folder)
    assert serena_binding.rewrite(_payload("find_symbol", {})) == {ROOT_ARG: str(repo["a"])}


@pytest.mark.feature("serena-hook-carries-each-agent-activated-root")
def test_an_activation_outside_a_checkout_keeps_the_previous_root(
    store: fakeredis.FakeRedis, repo: dict[str, Path], tmp_path: Path
) -> None:
    _activate(repo["a"])
    for project in ("relative/path", "", str(tmp_path / "missing"), str(tmp_path)):
        assert _activate(project) is None
    assert serena_binding.rewrite(_payload("find_symbol", {})) == {ROOT_ARG: str(repo["a"])}


@pytest.mark.feature("serena-hook-carries-each-agent-activated-root")
def test_calls_carry_nothing_without_an_activation_or_outside_serena(
    store: fakeredis.FakeRedis, repo: dict[str, Path]
) -> None:
    assert serena_binding.rewrite(_payload("find_symbol", {})) is None
    _activate(repo["a"])
    assert serena_binding.rewrite({**_payload("find_symbol", {}), "tool_name": "Read"}) is None
    assert serena_binding.rewrite({**_payload("find_symbol", {}), "session_id": "another-session"}) is None
    assert serena_binding.rewrite({**_payload("find_symbol", {}), "session_id": ""}) is None
    unkeyed = {**_payload("activate_project", {"project": str(repo["b"])}), "session_id": ""}
    assert serena_binding.rewrite(unkeyed) is None
    assert serena_binding.rewrite({**unkeyed, "session_id": SESSION, "tool_name": "activate_project"}) is None
    assert store.keys("*") == [redis_key("serena_root", SESSION)]


@pytest.mark.feature("serena-hook-carries-each-agent-activated-root")
def test_roots_live_in_one_session_hash_that_expires(store: fakeredis.FakeRedis, repo: dict[str, Path]) -> None:
    _activate(repo["a"])
    _activate(repo["b"], agent_id="sub-3")
    key = redis_key("serena_root", SESSION)
    assert store.hgetall(key) == {"": str(repo["a"]), "sub-3": str(repo["b"])}
    assert serena_binding.TTL_SECONDS == 7 * 24 * 3600
    assert serena_binding.TTL_SECONDS - 5 <= store.ttl(key) <= serena_binding.TTL_SECONDS


@pytest.mark.feature("serena-hook-carries-each-agent-activated-root")
def test_without_redis_nothing_is_recorded_or_carried(repo: dict[str, Path]) -> None:
    with patch("hooks._redis.get_redis", return_value=None):
        assert _activate(repo["a"]) is None
        assert serena_binding.rewrite(_payload("find_symbol", {})) is None


@pytest.mark.feature("serena-hook-carries-each-agent-activated-root")
def test_a_redis_error_records_and_carries_nothing(repo: dict[str, Path]) -> None:
    from redis import RedisError

    broken = MagicMock()
    broken.hset.side_effect = RedisError("down")
    broken.hgetall.side_effect = RedisError("down")
    with patch("hooks._redis.get_redis", return_value=broken):
        assert _activate(repo["a"]) is None
        assert serena_binding.rewrite(_payload("find_symbol", {})) is None
    broken.hset.assert_called_once_with(redis_key("serena_root", SESSION), "", str(repo["a"]))
    broken.hgetall.assert_called_once_with(redis_key("serena_root", SESSION))


@pytest.mark.feature("serena-hook-carries-each-agent-root-without-a-decision")
@pytest.mark.parametrize("mode", ["default", "acceptEdits", "plan", "dontAsk", "bypassPermissions"])
def test_pre_tool_use_carries_each_agent_root_without_a_permission_decision(
    store: fakeredis.FakeRedis,
    repo: dict[str, Path],
    forced_claude: None,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    hm.on_pre_tool_use(_payload("activate_project", {"project": str(repo["a"])}, mode=mode))
    hm.on_pre_tool_use(_payload("activate_project", {"project": str(repo["b"])}, agent_id="sub-1", mode=mode))
    capsys.readouterr()
    hm.on_pre_tool_use(_payload("replace_content", {"relative_path": "f.txt", "repl": "x"}, mode=mode))
    parent = json.loads(capsys.readouterr().out.strip())["hookSpecificOutput"]
    hm.on_pre_tool_use(_payload("find_symbol", {"name_path_pattern": "Binding"}, agent_id="sub-1", mode=mode))
    child = json.loads(capsys.readouterr().out.strip())["hookSpecificOutput"]
    parent.pop("additionalContext", None)
    child.pop("additionalContext", None)
    assert parent == {
        "hookEventName": "PreToolUse",
        "updatedInput": {"relative_path": "f.txt", "repl": "x", ROOT_ARG: str(repo["a"])},
    }
    assert child == {
        "hookEventName": "PreToolUse",
        "updatedInput": {"name_path_pattern": "Binding", ROOT_ARG: str(repo["b"])},
    }


@pytest.mark.feature("serena-hook-carries-each-agent-root-without-a-decision")
def test_pre_tool_use_carries_no_root_in_auto_mode_or_on_other_targets(
    store: fakeredis.FakeRedis,
    repo: dict[str, Path],
    forced_claude: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hm.on_pre_tool_use(_payload("activate_project", {"project": str(repo["a"])}, mode="auto"))
    hm.on_pre_tool_use(_payload("find_symbol", {}, mode="auto"))
    assert ROOT_ARG not in capsys.readouterr().out
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "copilot")
    hm.on_pre_tool_use(_payload("find_symbol", {}, mode="default"))
    assert ROOT_ARG not in capsys.readouterr().out
    assert hm._input_rewrite(_payload("find_symbol", {}, mode="default"), "mcp__serena__find_symbol") is None
    assert store.hgetall(redis_key("serena_root", SESSION)) == {"": str(repo["a"])}


@pytest.mark.feature("serena-hook-carries-each-agent-root-without-a-decision")
@pytest.mark.parametrize("decision", ["allow", "ask"])
def test_a_condition_decision_carries_the_root_on_its_own_decision(
    store: fakeredis.FakeRedis,
    repo: dict[str, Path],
    forced_claude: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    decision: str,
) -> None:
    from hooks.context import conditions

    def decide(_given: dict[str, Any]) -> conditions.PreEffect:
        return conditions.PreEffect(contexts=[], decision=decision)

    monkeypatch.setattr(conditions, "pre_effect", decide)
    hm.on_pre_tool_use(_payload("activate_project", {"project": str(repo["a"])}, mode="default"))
    capsys.readouterr()
    hm.on_pre_tool_use(_payload("find_symbol", {"name_path_pattern": "Binding"}, mode="default"))
    out = json.loads(capsys.readouterr().out.strip())["hookSpecificOutput"]
    assert out["permissionDecision"] == decision
    assert out.get("permissionDecisionReason", "") == ""
    assert out["updatedInput"] == {"name_path_pattern": "Binding", ROOT_ARG: str(repo["a"])}


@pytest.mark.feature("serena-hook-carries-each-agent-activated-root")
def test_the_serena_off_switch_records_and_carries_nothing(
    store: fakeredis.FakeRedis,
    repo: dict[str, Path],
    forced_claude: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("hooks.config.SERENA_EDIT_GUARD_ENABLED", False)
    hm.on_pre_tool_use(_payload("activate_project", {"project": str(repo["a"])}))
    hm.on_pre_tool_use(_payload("find_symbol", {}))
    assert ROOT_ARG not in capsys.readouterr().out
    assert store.hgetall(redis_key("serena_root", SESSION)) == {}


def test_a_condition_allow_without_an_input_patch_emits_no_reason_or_input(
    store: fakeredis.FakeRedis,
    forced_claude: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from hooks.context import conditions

    def allow(_given: dict[str, Any]) -> conditions.PreEffect:
        return conditions.PreEffect(contexts=[], decision="allow")

    monkeypatch.setattr(conditions, "pre_effect", allow)
    hm.on_pre_tool_use(_payload("find_symbol", {}))
    out = json.loads(capsys.readouterr().out.strip())["hookSpecificOutput"]
    assert out["permissionDecision"] == "allow"
    assert out.get("permissionDecisionReason", "") == ""
    assert "updatedInput" not in out
