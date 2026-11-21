import io
import sys
from typing import Any
from unittest.mock import Mock

import pytest

from hooks import config, hook_manager
from hooks.context import branch_guard, master_coding, venv_guard
from hooks.hook_manager import BlockAction

pytestmark = pytest.mark.unit

GUARDS = (
    (branch_guard, "check_branch_guard", "branch_guard"),
    (branch_guard, "check_commit_on_main", "commit_on_main"),
    (master_coding, "protection_refusal", "master_coding"),
    (venv_guard, "check_venv_guard", "venv_guard"),
)


class FlushedStream(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flushed: list[str] = []

    def flush(self) -> None:
        self.flushed.append(self.getvalue())
        super().flush()


@pytest.fixture
def pipeline(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    for flag in (
        "QUOTA_POLICY_ENABLED",
        "BROADCAST_ENABLED",
        "ENFORCEMENT_INJECTION_ENABLED",
        "RETRY_BREAKER_ENABLED",
        "BRAIN_ENABLED",
    ):
        monkeypatch.setattr(config, flag, False)
    monkeypatch.setattr(config, "SECRETS_MODE", "off")
    return {"tool_name": "Bash", "tool_input": {"command": "echo ready"}, "session_id": "shell-failure"}


@pytest.mark.feature("shell-guard-failures-are-isolated")
@pytest.mark.parametrize("failed_guard", range(len(GUARDS)))
def test_pretool_runs_every_shell_guard_and_names_only_the_crashed_guard(
    monkeypatch: pytest.MonkeyPatch, pipeline: dict[str, Any], failed_guard: int
) -> None:
    calls: list[str] = []
    logged = Mock()
    stream = FlushedStream()
    monkeypatch.setattr(hook_manager, "log", logged)
    monkeypatch.setattr(sys, "stderr", stream)

    def record(received: dict[str, Any], name: str) -> None:
        assert received is pipeline
        calls.append(name)
        if name == GUARDS[failed_guard][2]:
            raise RuntimeError("offline")

    for module, symbol, name in GUARDS:
        monkeypatch.setattr(module, symbol, Mock(side_effect=lambda received, name=name: record(received, name)))

    hook_manager.on_pre_tool_use(pipeline)

    assert calls == ["branch_guard", "commit_on_main", "master_coding", "venv_guard"]
    name = GUARDS[failed_guard][2]
    logged.assert_any_call(f"{name} check failed", {"error": "offline"})
    assert stream.flushed == [f"WARNING: {name} check failed (offline) — guard bypassed\n"]


@pytest.mark.feature("shell-guard-failures-are-isolated")
@pytest.mark.parametrize("denied_guard", range(len(GUARDS)))
def test_pretool_preserves_each_shell_guard_denial(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], pipeline: dict[str, Any], denied_guard: int
) -> None:
    guards = []
    for index, (module, symbol, _) in enumerate(GUARDS):
        guard = Mock(side_effect=BlockAction("denied")) if index == denied_guard else Mock(return_value=None)
        monkeypatch.setattr(module, symbol, guard)
        guards.append(guard)

    with pytest.raises(BlockAction) as error:
        hook_manager.on_pre_tool_use(pipeline)

    assert str(error.value) == "denied"
    assert [guard.call_count for guard in guards] == [int(index <= denied_guard) for index in range(len(GUARDS))]
    assert capsys.readouterr().err == ""


@pytest.mark.feature("shell-guard-failures-are-isolated")
def test_pretool_still_blocks_a_protection_refusal(monkeypatch: pytest.MonkeyPatch, pipeline: dict[str, Any]) -> None:
    for module, symbol, _ in GUARDS:
        monkeypatch.setattr(module, symbol, Mock(return_value=None))
    monkeypatch.setattr(master_coding, "protection_refusal", Mock(return_value="protected"))

    with pytest.raises(BlockAction) as error:
        hook_manager.on_pre_tool_use(pipeline)

    assert str(error.value) == "protected"
