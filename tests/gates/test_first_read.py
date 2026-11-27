import json
import re
from pathlib import Path

import pytest

from scripts.gates import entry, first_read
from scripts.gates.base import Call, Who
from scripts.profiles import binding

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
    assert decision.allowed is False
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
        'const r = await tools.exec_command({cmd: "agentihooks seat brief"}); await tools.apply_patch(r);',
        "/opt/bin/other seat brief",
        "agentihooks seat status",
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
    upper = command.replace("abc123", "ABC123")
    assert (
        first_read.FirstRead().decide(Call(tool="Bash", tool_input={"command": upper}, session="s"), ME, None).allowed
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
    assert gate.decide(Call(tool="Read", tool_input={"path": str(own)}, session="s"), ME, None).allowed
    assert not gate.decide(Call(tool="Write", tool_input={"file_path": str(own)}, session="s"), ME, None).allowed
    assert not gate.decide(Call(tool="Read", session="s"), ME, None).allowed


@pytest.mark.feature("first-read-gate")
def test_quick_seat_opening_prompt_uses_the_launcher_safe_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTIHOOKS_QUICK_SEAT", "eng:one")
    prompt = tmp_path / "agentihooks-claude-terminal" / "eng_one-261126-195500-123.prompt"
    call = Call(tool="Read", tool_input={"file_path": str(prompt)}, session="s")
    assert first_read.FirstRead().decide(call, Who(), None).allowed


@pytest.mark.feature("first-read-gate")
def test_decision_uses_the_given_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = first_read.FirstRead()
    call = Call(tool="Write", session="given")
    assert gate.decide(call, Who(), None).allowed
    monkeypatch.delenv("AGENTIHOOKS_SWARM")
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME")
    assert gate.decide(call, ME, None).allowed is False


@pytest.mark.feature("first-read-gate")
def test_marker_is_isolated_to_its_session() -> None:
    path = first_read.marker_path("session-one")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1")
    gate = first_read.FirstRead()
    assert not gate.matches(Call(tool="Write", session="session-one"))
    assert gate.decide(Call(tool="Write", session="session-one"), ME, None).allowed
    assert not gate.decide(Call(tool="Write", session="session-two"), ME, None).allowed
    with pytest.raises(ValueError, match="^invalid first read session id$"):
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
    assert first_read.marker_path("session") == Path.home() / ".agentihooks" / "first-read" / "session"
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
        "python -c 'import sys; x = sys.path.insert(0, \"/repo\"); from scripts.profiles.binding import main; sys.exit(main(sys.argv[2]))' --canary abc123",
        "python -c 'import sys; print(0, \"/repo\"); from scripts.profiles.binding import main; sys.exit(main(sys.argv[2]))' --canary abc123",
        "python -c 'import sys; from scripts.profiles.binding import main; sys.exit(main(sys.argv[2]))' --canary abc123 extra",
        "ruby -c 'import sys; from scripts.profiles.binding import main; sys.exit(main(sys.argv[2]))' --canary abc123",
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


@pytest.mark.feature("first-read-gate")
@pytest.mark.parametrize(
    "command",
    [
        "$(id)/agentihooks seat brief",
        "<(id)/agentihooks seat brief",
        ">(id)/agentihooks seat brief",
        "<(id)/python -c 'import sys; from scripts.profiles.binding import main; sys.exit(main(sys.argv[2]))' --canary abc123",
        "\u0060id\u0060/agentihooks seat brief",
        "$(id)/python -c 'import sys; from scripts.profiles.binding import main; sys.exit(main(sys.argv[2]))' --canary abc123",
        "python -c 'import sys; from scripts.profiles.binding import main; sys.exit(main(sys.argv[2]))' --canary $(id)",
    ],
)
def test_shell_expansion_is_not_a_bootstrap_command(command: str) -> None:
    assert (
        not first_read.FirstRead()
        .decide(Call(tool="Bash", tool_input={"command": command}, session="s"), ME, None)
        .allowed
    )


@pytest.mark.feature("first-read-gate")
def test_registry_identity_is_a_member(monkeypatch: pytest.MonkeyPatch) -> None:
    def registry() -> Who:
        return ME

    monkeypatch.delenv("AGENTIHOOKS_SWARM")
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME")
    monkeypatch.setattr(first_read.Who, "from_env", registry)
    first_read.reset({"session_id": "registry", "source": "startup"})
    assert first_read.membership_path("registry").read_text() == "1"
    assert first_read.FirstRead().matches(Call(tool="Write", session="registry"))
    first_read.mark(
        {
            "session_id": "registry",
            "tool_name": "Bash",
            "tool_input": {"command": "agentihooks seat brief"},
            "tool_response": {"exit_code": 0},
        }
    )
    assert first_read.marker_path("registry").read_text() == "1"


CANARY_HEAD = "python -c 'import sys; sys.path.insert(0, "
CANARY_TAIL = "); from scripts.profiles.binding import main; sys.exit(main(sys.argv[2]))' --canary abc123"
MOUNTED_CANARY = (
    "/opt/venv/bin/python -c 'import sys; sys.path.insert(0, '\"'\"'/opt/venv/lib/python3.12/site-packages'\"'\"');"
    " from scripts.profiles.binding import main; sys.exit(main(sys.argv[2]))' --canary 0123456789abcdef01234567"
)


def _bootstrap(command: str) -> bool:
    return (
        first_read.FirstRead().decide(Call(tool="Bash", tool_input={"command": command}, session="s"), ME, None).allowed
    )


@pytest.mark.feature("first-read-canary-literal")
def test_profile_binding_canary_is_exempt() -> None:
    found = re.search(r"execute `(.+)` through your shell tool", binding.persona("profile"))
    assert found
    assert _bootstrap(found.group(1))


@pytest.mark.feature("first-read-canary-literal")
@pytest.mark.parametrize(
    "command",
    [
        MOUNTED_CANARY,
        f"const r = await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)}}}); text(r);",
        f"text(await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)}}}));",
        f"text(await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)}, max_output_tokens: 4000, yield_time_ms: 10000}}));",
        f'const r = await tools.exec_command({{"cmd": {json.dumps(MOUNTED_CANARY)}, "yield_time_ms": 1000}}); text(r);',
        f"text(await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)},}}));",
    ],
)
def test_mounted_literal_canary_is_exempt(command: str) -> None:
    assert _bootstrap(command)


@pytest.mark.feature("first-read-canary-literal")
@pytest.mark.parametrize(
    "pragma",
    [
        '// @exec: {"yield_time_ms": 10000, "max_output_tokens": 1000}\n',
        ' \t// @exec: {"yield_time_ms": 1000}\r\n',
    ],
    ids=["lf", "indented-crlf"],
)
@pytest.mark.parametrize(
    "program",
    [
        f"await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)}}});",
        f"const r = await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)}}}); text(r);",
        f"text(await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)}, max_output_tokens: 4000, yield_time_ms: 10000}}));",
    ],
    ids=["bare", "bound", "text"],
)
def test_exec_pragma_canary_is_exempt(pragma: str, program: str) -> None:
    assert _bootstrap(pragma + program)


@pytest.mark.feature("first-read-canary-literal")
@pytest.mark.parametrize(
    "program",
    [
        f"// @exec: {{}}\ntext(await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)}}})); fetch(1);",
        f"// @exec: {{}}\nfetch(1); text(await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)}}}));",
        f'// @exec: {{}}\ntext(await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)}}})); await tools.exec_command({{cmd: "pwd"}});',
        f"// @exec: {{}}\r\nconst r = await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)}}}); text(r); fetch(r);",
        f"// @exec: {{}}\n// @exec: {{}}\ntext(await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)}}}));",
        f"// @exec: {{}}text(await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)}}}));",
        f"// @exec: {{}}\u2028fetch(1);\ntext(await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)}}}));",
        f"// @exec: {{}}\u2029fetch(1);\ntext(await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)}}}));",
        f"// other comment\ntext(await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)}}}));",
    ],
    ids=[
        "suffix",
        "prefix",
        "second-call",
        "crlf-suffix",
        "two-pragmas",
        "no-newline",
        "line-separator",
        "paragraph-separator",
        "foreign-comment",
    ],
)
def test_exec_pragma_cannot_hide_other_work(program: str) -> None:
    decision = first_read.FirstRead().decide(Call(tool="Bash", tool_input={"command": program}, session="s"), ME, None)
    assert decision.allowed is False
    assert "agentihooks seat brief" in decision.reason


@pytest.mark.feature("first-read-canary-literal")
@pytest.mark.parametrize(
    "command",
    [
        CANARY_HEAD + '"\'"$(id)"\'"' + CANARY_TAIL,
        CANARY_HEAD + '"\'"\u0060id\u0060"\'"' + CANARY_TAIL,
        CANARY_HEAD + "\"'\"'$(id)'\"'\"" + CANARY_TAIL,
        CANARY_HEAD + "\"'$(id)'\"" + CANARY_TAIL,
        CANARY_HEAD + "\"'<(id)'\"" + CANARY_TAIL,
        CANARY_HEAD + "\"'\\'$(id)'\"" + CANARY_TAIL,
        "python -c \"import sys; sys.path.insert(0, '$(id)'); from scripts.profiles.binding import main; sys.exit(main(sys.argv[2]))\" --canary abc123",
        "/opt/ven\\v/bin/python -c 'import sys; from scripts.profiles.binding import main; sys.exit(main(sys.argv[2]))' --canary abc123",
        "/op?/venv/bin/python -c 'import sys; from scripts.profiles.binding import main; sys.exit(main(sys.argv[2]))' --canary abc123",
        "~/venv/bin/python -c 'import sys; from scripts.profiles.binding import main; sys.exit(main(sys.argv[2]))' --canary abc123",
        f'await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)} + "$(id)"}});',
        f"await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)}, ...extra}});",
        f"await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)}, cmd: other}});",
        f'require("child_process").execSync("id"); await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)}}});',
        f"const r = await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)}}}); text(r); fetch(r);",
        f'await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)}, shell: "/home/worker/x/evil"}});',
        f"await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)}}}, evil);",
        f'text(await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)}, workdir: "/home/worker/x"}}));',
        f"text(await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)}, max_output_tokens: limit}}));",
        f"text(await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)}, max_output_tokens: }}));",
        f"text(await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)}}})); fetch(1);",
        f'text(await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)}, cmd: "pwd"}}));',
        f'await tools.exec_command({{cmd: {json.dumps(MOUNTED_CANARY)}}}); await tools.exec_command({{cmd: "pwd"}});',
    ],
)
def test_canary_with_shell_expansion_requires_brief(command: str) -> None:
    decision = first_read.FirstRead().decide(Call(tool="Bash", tool_input={"command": command}, session="s"), ME, None)
    assert decision.allowed is False
    assert "agentihooks seat brief" in decision.reason
