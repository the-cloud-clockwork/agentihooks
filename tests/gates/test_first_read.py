import json
from pathlib import Path

import pytest

from scripts.gates import entry, first_read
from scripts.gates.base import Call, Who

ME = Who(name="engineer@123-456", swarm="proof", lane="eng")


@pytest.fixture(autouse=True)
def seated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "agentihooks"))
    monkeypatch.setenv("AGENTIHOOKS_SWARM", ME.swarm)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", ME.name)
    monkeypatch.delenv("AGENTIHOOKS_QUICK_SEAT", raising=False)


@pytest.mark.feature("first-read-gate")
def test_unread_seat_requires_brief() -> None:
    call = Call(tool="Bash", tool_input={"command": "pwd"}, session="session-one")
    gate = first_read.FirstRead()
    assert gate.matches(call)
    decision = gate.decide(call, ME, None)
    assert not decision.allowed
    assert decision.reason == "Run agentihooks seat brief before using other tools in this seated session."


@pytest.mark.feature("first-read-gate")
def test_standalone_is_outside_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTIHOOKS_SWARM")
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME")
    call = Call(tool="Write", session="standalone")
    assert not first_read.FirstRead().matches(call)
    assert first_read.FirstRead().decide(call, Who(), None).allowed


@pytest.mark.feature("first-read-gate")
def test_quick_seat_is_included(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTIHOOKS_SWARM")
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME")
    monkeypatch.setenv("AGENTIHOOKS_QUICK_SEAT", "engineer-one")
    call = Call(tool="Read", tool_input={"file_path": "/repo/code.py"}, session="quick")
    assert first_read.FirstRead().matches(call)
    assert not first_read.FirstRead().decide(call, Who(), None).allowed


@pytest.mark.feature("first-read-gate")
@pytest.mark.parametrize(
    "tool",
    [
        "ToolSearch",
        "mcp__agentihooks__condition_list",
        "mcp__agentihooks__condition_show",
        "mcp__agentihooks__condition_set",
        "mcp__agentihooks__condition_clear",
    ],
)
def test_bootstrap_tools_are_exempt(tool: str) -> None:
    assert first_read.FirstRead().decide(Call(tool=tool, session="s"), ME, None).allowed


@pytest.mark.feature("first-read-gate")
@pytest.mark.parametrize(
    "command",
    [
        "agentihooks seat brief",
        "/opt/bin/agentihooks seat brief",
        'const r = await tools.exec_command({cmd: "agentihooks seat brief"}); text(r);',
    ],
)
def test_brief_command_is_exempt(command: str) -> None:
    assert first_read.is_brief(command)
    call = Call(tool="Bash", tool_input={"command": command}, session="s")
    assert first_read.FirstRead().decide(call, ME, None).allowed


@pytest.mark.feature("first-read-gate")
@pytest.mark.parametrize(
    "command",
    [
        "echo agentihooks seat brief",
        "agentihooks seat brief; pwd",
        "agentihooks seat brief && pwd",
        "agentihooks seat brief > /repo/output",
        'const r = await tools.exec_command({cmd: "pwd"}); text("agentihooks seat brief");',
    ],
)
def test_brief_words_do_not_exempt_other_commands(command: str) -> None:
    assert not first_read.is_brief(command)
    assert (
        not first_read.FirstRead()
        .decide(Call(tool="Bash", tool_input={"command": command}, session="s"), ME, None)
        .allowed
    )


@pytest.mark.feature("first-read-gate")
def test_canary_is_exempt() -> None:
    command = (
        "python -c 'import sys; from scripts.profiles.binding import main; sys.exit(main(sys.argv[2]))' --canary abc123"
    )
    assert (
        first_read.FirstRead().decide(Call(tool="Bash", tool_input={"command": command}, session="s"), ME, None).allowed
    )


@pytest.mark.feature("first-read-gate")
def test_only_own_opening_prompt_is_exempt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    folder = tmp_path / "agentihooks-claude-terminal"
    own = folder / "engineer-123-456-261126-195500-123.prompt"
    other = folder / "engineer-999-999-261126-195500-123.prompt"
    gate = first_read.FirstRead()
    assert gate.decide(Call(tool="Read", tool_input={"file_path": str(own)}, session="s"), ME, None).allowed
    assert not gate.decide(Call(tool="Read", tool_input={"file_path": str(other)}, session="s"), ME, None).allowed
    assert gate.decide(
        Call(tool="Read", tool_input={"file_path": str(own), "offset": 1, "limit": 1}, session="s"), ME, None
    ).allowed


@pytest.mark.feature("first-read-gate")
def test_marker_is_isolated_to_its_session() -> None:
    path = first_read.marker_path("session-one")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1")
    gate = first_read.FirstRead()
    assert not gate.matches(Call(tool="Write", session="session-one"))
    assert gate.decide(Call(tool="Write", session="session-one"), ME, None).allowed
    assert not gate.decide(Call(tool="Write", session="session-two"), ME, None).allowed
    with pytest.raises(ValueError):
        first_read.marker_path("../session-one")


@pytest.mark.feature("first-read-gate")
def test_registry_default_and_swarm_key() -> None:
    from scripts.gates import catalog
    from scripts.swarm.cli import GATE_KEYS

    assert entry.GATES["first-read"].default_mode == "observe"
    assert catalog.defaults()["first-read"] == "observe"
    assert GATE_KEYS["first-read-gate"] == "first-read"


@pytest.mark.feature("first-read-gate")
def test_evaluation_fails_open_on_marker_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def broken(session: str) -> Path:
        raise OSError("marker unavailable")

    monkeypatch.setattr(first_read, "marker_path", broken)
    monkeypatch.setattr(entry.modes, "swarm_gates", lambda *args: {})
    monkeypatch.setenv("AGENTIHOOKS_GATE_FIRST_READ", "enforce")
    call = Call(tool="Bash", tool_input={"command": "pwd"}, session="s")
    assert entry.evaluate(first_read.FirstRead(), call, home=tmp_path) == 0
    rows = list(tmp_path.rglob("log.jsonl"))
    assert rows
    assert json.loads(rows[0].read_text().splitlines()[-1])["kind"] == "fail-open"


@pytest.mark.feature("first-read-gate")
def test_existing_mode_and_standalone_contracts_are_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.gates.identity import PinnedIdentity

    monkeypatch.setattr(entry.modes, "swarm_gates", lambda *args: {})
    monkeypatch.setenv("AGENTIHOOKS_GATE_IDENTITY", "off")
    assert entry.evaluate(PinnedIdentity(), Call(tool="Bash", tool_input={"command": "pwd"}), {}) == 0


@pytest.mark.feature("first-read-gate")
def test_marker_home_default_and_empty_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AGENTIHOOKS_HOME")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert first_read.marker_path("session") == tmp_path / ".agentihooks" / "first-read" / "session"
    assert not first_read.FirstRead().matches(Call(tool="Write"))
    assert first_read.FirstRead().decide(Call(tool="Write"), ME, None).allowed


@pytest.mark.feature("first-read-gate")
def test_mounted_canary_path_setup_is_exempt() -> None:
    command = "python -c 'import sys; sys.path.insert(0, \"/repo\"); from scripts.profiles.binding import main; sys.exit(main(sys.argv[2]))' --canary abc123"
    assert (
        first_read.FirstRead().decide(Call(tool="Bash", tool_input={"command": command}, session="s"), ME, None).allowed
    )


@pytest.mark.feature("first-read-gate")
@pytest.mark.parametrize(
    "command",
    [
        "python -c 'import sys; from other import main; sys.exit(main(sys.argv[2]))' --canary abc123",
        "python -c 'import os; from scripts.profiles.binding import main; sys.exit(main(sys.argv[2]))' --canary abc123",
        "python -c 'x = 1; from scripts.profiles.binding import main; sys.exit(main(sys.argv[2]))' --canary abc123",
        "python -c 'sys.path.insert(1, \"/repo\"); from scripts.profiles.binding import main; sys.exit(main(sys.argv[2]))' --canary abc123",
        "python -c 'print(1); from scripts.profiles.binding import main; sys.exit(main(sys.argv[2]))' --canary abc123",
        "python -c 'if' --canary abc123",
        "python -c 'from scripts.profiles.binding import main' --canary abc123",
        "python -c 'import sys; from scripts.profiles.binding import main; sys.exit(main(sys.argv[2]))' --other abc123",
        "python -c '",
        'await tools.exec_command({cmd: "agentihooks seat brief"}); await tools.exec_command({cmd: "pwd"});',
    ],
)
def test_bootstrap_cannot_hide_other_work(command: str) -> None:
    assert (
        not first_read.FirstRead()
        .decide(Call(tool="Bash", tool_input={"command": command}, session="s"), ME, None)
        .allowed
    )


@pytest.mark.feature("first-read-gate")
def test_similar_tool_and_foreign_prompt_are_not_exempt(tmp_path: Path) -> None:
    gate = first_read.FirstRead()
    assert not gate.decide(Call(tool="mcp__other__condition_show", session="s"), ME, None).allowed
    assert not gate.decide(
        Call(tool="Read", tool_input={"file_path": str(tmp_path / "opening.prompt")}, session="s"), ME, None
    ).allowed
