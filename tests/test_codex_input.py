import sys

import pytest

from scripts import codex_input, init_agent


@pytest.mark.parametrize("resume", ["", "conversation"])
def test_swarm_codex_launch_guards_the_routed_command(tmp_path, resume):
    spec = init_agent.AgentSpec(agent="codex", resume=resume)
    plain, before = init_agent._agent_command(spec, tmp_path / "route", "probe", [], {}, tmp_path)
    guarded, guarded_before = init_agent._agent_command(
        spec, tmp_path / "route", "probe", [], {"AGENTIHOOKS_SWARM": "proof"}, tmp_path
    )
    assert guarded == [sys.executable, "-m", "scripts.codex_input", "--", *plain]
    assert guarded_before == before == ""


def test_swarm_claude_launch_keeps_its_command(tmp_path):
    spec = init_agent.AgentSpec()
    plain = init_agent._agent_command(spec, tmp_path / "route", "probe", [], {}, tmp_path)
    assert (
        init_agent._agent_command(spec, tmp_path / "route", "probe", [], {"AGENTIHOOKS_SWARM": "proof"}, tmp_path)
        == plain
    )


@pytest.mark.parametrize(
    ("sequence", "expected"),
    [
        (b"\x1b[<35;100;30M", b""),
        (b"\x1b[<0;100;30m", b""),
        (b"\x1b[MCd2", b""),
        (b"\x1b[35;100;30M", b""),
        (b"\x1b[3~", b"\x1b[3~"),
        (b"\x1b[1;5D", b"\x1b[1;5D"),
        (b"\x1bOA", b"\x1bOA"),
        (b"\x1b[Z", b"\x1b[Z"),
        (b"\x1b[97;5u", b"\x1b[97;5u"),
    ],
)
def test_delayed_fragments_are_mouse_reports_or_complete_keys(monkeypatch, sequence, expected):
    now = [0.0]
    monkeypatch.setattr(codex_input.time, "monotonic", lambda: now[0])
    framer = codex_input.InputFramer()
    output = bytearray()
    for value in sequence:
        output.extend(framer.feed(bytes([value])))
        now[0] += 0.3
        output.extend(framer.expire_escape())
    assert bytes(output) == expected
    assert framer.feed(b"ordinary") == b"ordinary"


def test_bare_escape_is_released_after_the_fragment_window(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(codex_input.time, "monotonic", lambda: now[0])
    framer = codex_input.InputFramer()
    assert framer.feed(b"\x1b") == b""
    now[0] = 0.999
    assert framer.expire_escape() == b""
    now[0] = 1.0
    assert framer.expire_escape() == b"\x1b"
    assert framer.expire_escape() == b""
    assert framer.feed(b"text") == b"text"


def test_incomplete_csi_is_never_flushed_as_text(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(codex_input.time, "monotonic", lambda: now[0])
    framer = codex_input.InputFramer()
    assert framer.feed(b"\x1b[") == b""
    now[0] = 100
    assert framer.expire_escape() == b""
    assert framer.feed(b"3~") == b"\x1b[3~"


@pytest.mark.parametrize("prefix", [b"\x1b", b"\x1b[", b"\x1b[<35;"])
def test_interrupt_discards_an_unfinished_sequence(prefix):
    framer = codex_input.InputFramer()
    assert framer.feed(prefix) == b""
    assert framer.feed(b"\x03text") == b"\x03text"


def test_keys_and_unicode_text_are_preserved():
    framer = codex_input.InputFramer()
    data = "héllo".encode() + b"\r\t\x7f\x1bx"
    assert framer.feed(data) == data


def test_a_new_escape_replaces_an_unfinished_csi():
    framer = codex_input.InputFramer()
    assert framer.feed(b"\x1b[") == b""
    assert framer.feed(b"\x1b[D") == b"\x1b[D"


def test_two_escape_keys_preserve_the_first_and_restart_the_timer(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(codex_input.time, "monotonic", lambda: now[0])
    framer = codex_input.InputFramer()
    assert framer.feed(b"\x1b") == b""
    now[0] = 0.6
    assert framer.feed(b"\x1b") == b"\x1b"
    now[0] = 1.1
    assert framer.expire_escape() == b""
    now[0] = 1.6
    assert framer.expire_escape() == b"\x1b"


def test_bracketed_paste_preserves_mouse_text_and_control_bytes():
    data = b"\x1b[200~paste\x1b[MCd2\x1b[<35;100;30M\x03\x1b[\x1b\x1b[201~"
    for split in range(len(data) + 1):
        framer = codex_input.InputFramer()
        assert framer.feed(data[:split]) + framer.feed(data[split:]) == data
        assert framer.feed(b"\x1b[<35;100;30Mafter") == b"after"


def test_app_server_launch_has_no_terminal_guard(tmp_path):
    spec = init_agent.AgentSpec(agent="codex", transport=init_agent.APP_SERVER)
    plain = init_agent._agent_command(spec, tmp_path / "route", "probe", [], {}, tmp_path)
    assert (
        init_agent._agent_command(spec, tmp_path / "route", "probe", [], {"AGENTIHOOKS_SWARM": "proof"}, tmp_path)
        == plain
    )
