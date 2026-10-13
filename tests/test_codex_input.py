import os
import pty
import select
import signal
import subprocess
import sys
import termios
import time
from contextlib import contextmanager

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


def test_delayed_paste_terminator_does_not_expire_as_an_escape_key(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(codex_input.time, "monotonic", lambda: now[0])
    framer = codex_input.InputFramer()
    assert framer.feed(b"\x1b[200~body\x1b") == b"\x1b[200~body"
    now[0] = 100.0
    assert framer.expire_escape() == b""
    assert framer.feed(b"[201~after") == b"\x1b[201~after"
    assert framer.feed(b"\x1b[MCd2") == b""


def test_app_server_launch_has_no_terminal_guard(tmp_path):
    spec = init_agent.AgentSpec(agent="codex", transport=init_agent.APP_SERVER)
    plain = init_agent._agent_command(spec, tmp_path / "route", "probe", [], {}, tmp_path)
    assert (
        init_agent._agent_command(spec, tmp_path / "route", "probe", [], {"AGENTIHOOKS_SWARM": "proof"}, tmp_path)
        == plain
    )


@pytest.mark.parametrize("escape", [b"\x1b[27u", b"\x1b[27;1u", b"\x1b[27;1:1u"])
@pytest.mark.parametrize(("tail", "expected"), [(b"[<35;100;30M", b""), (b"[MCd2", b""), (b"[3~", b"\x1b[3~")])
def test_kitty_escape_before_orphan_tails_is_reassembled(monkeypatch, escape, tail, expected):
    now = [0.0]
    monkeypatch.setattr(codex_input.time, "monotonic", lambda: now[0])
    framer = codex_input.InputFramer()
    assert framer.feed(escape) == b""
    now[0] = 0.3
    assert framer.expire_escape() == b""
    assert framer.feed(tail[:1]) == b""
    now[0] = 0.6
    assert framer.expire_escape() == b""
    assert framer.feed(tail[1:]) == expected


def test_kitty_escape_key_keeps_its_encoding_when_it_is_not_a_fragment(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(codex_input.time, "monotonic", lambda: now[0])
    framer = codex_input.InputFramer()
    assert framer.feed(b"\x1b[27u") == b""
    now[0] = 1.0
    assert framer.expire_escape() == b"\x1b[27u"
    assert framer.feed(b"\x1b[27u" + b"text") == b"\x1b[27utext"


@contextmanager
def _relay(child):
    master, slave = pty.openpty()
    saved = termios.tcgetattr(slave)
    termios.tcsetwinsize(slave, (55, 160))
    process = subprocess.Popen(
        [sys.executable, "-m", "scripts.codex_input", "--", sys.executable, "-u", "-c", child],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    try:
        yield process, master, slave, saved
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        os.close(master)
        os.close(slave)


def _read_until(fd, marker):
    output = bytearray()
    deadline = time.monotonic() + 5
    while marker not in output and time.monotonic() < deadline:
        if select.select([fd], [], [], 0.05)[0]:
            output.extend(os.read(fd, 65536))
    assert marker in output, bytes(output)
    return bytes(output)


@pytest.mark.parametrize(
    ("escape", "tail", "expected"),
    [
        (b"\x1b", b"[<35;100;30M", b"text"),
        (b"\x1b[27u", b"[MCd2", b"text"),
        (b"\x1b[27u", b"[3~", b"\x1b[3~text"),
    ],
)
def test_terminal_child_gets_clean_delayed_input_and_its_exit_status(escape, tail, expected):
    child = (
        "import os,sys,tty; tty.setraw(0); print('READY', flush=True); data=b''\n"
        "while not data.endswith(b'END'):\n data += os.read(0,4096)\n"
        "print('HEX '+data[:-3].hex(),flush=True); sys.exit(23)"
    )
    with _relay(child) as (process, master, slave, saved):
        _read_until(master, b"READY")
        os.write(master, escape)
        time.sleep(0.3)
        os.write(master, tail[:1])
        time.sleep(0.3)
        os.write(master, tail[1:] + b"textEND")
        output = _read_until(master, b"HEX ")
        assert b"HEX " + expected.hex().encode() in output
        assert process.wait(timeout=5) == 23
        assert termios.tcgetattr(slave) == saved


def test_terminal_child_gets_paste_and_interrupt_bytes():
    child = (
        "import os,tty; tty.setraw(0); print('READY',flush=True); data=b''\n"
        "while not data.endswith(b'END'):\n data+=os.read(0,4096)\n"
        "print('HEX '+data[:-3].hex(),flush=True)"
    )
    data = b"\x1b[200~mouse\x1b[<35;100;30M\x1b[201~\x03"
    with _relay(child) as (process, master, slave, saved):
        _read_until(master, b"READY")
        os.write(master, data + b"END")
        assert b"HEX " + data.hex().encode() in _read_until(master, b"HEX ")
        assert process.wait(timeout=5) == 0


def test_terminal_child_receives_startup_size_and_resize():
    child = (
        "import signal,termios,time\n"
        "def size(*args):\n print('SIZE '+str(termios.tcgetwinsize(0)),flush=True)\n"
        "signal.signal(signal.SIGWINCH,size); size(); time.sleep(5)"
    )
    with _relay(child) as (process, master, slave, saved):
        _read_until(master, b"SIZE (55, 160)")
        termios.tcsetwinsize(slave, (40, 100))
        os.kill(process.pid, signal.SIGWINCH)
        _read_until(master, b"SIZE (40, 100)")
        os.kill(process.pid, signal.SIGTERM)
        assert process.wait(timeout=5) == 143
        assert termios.tcgetattr(slave) == saved


def test_signal_interrupt_reaches_the_child_and_restores_the_terminal():
    child = "import time; print('READY',flush=True); time.sleep(5)"
    with _relay(child) as (process, master, slave, saved):
        _read_until(master, b"READY")
        os.kill(process.pid, signal.SIGINT)
        assert process.wait(timeout=5) == 130
        assert termios.tcgetattr(slave) == saved


def test_nonterminal_launch_preserves_stdin_and_exit_status():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.codex_input",
            "--",
            sys.executable,
            "-c",
            "import sys; print(sys.stdin.read()); sys.exit(19)",
        ],
        input="piped input",
        text=True,
        capture_output=True,
        timeout=5,
    )
    assert result.stdout == "piped input\n"
    assert result.stderr == ""
    assert result.returncode == 19


def test_partial_terminal_writes_preserve_every_byte(monkeypatch):
    reader, writer = os.pipe()
    real_write = os.write

    def partial(fd, data):
        assert fd == writer
        return real_write(fd, data[:2])

    monkeypatch.setattr(codex_input.os, "write", partial)
    try:
        codex_input.write_all(writer, b"terminal output")
        assert os.read(reader, 64) == b"terminal output"
    finally:
        os.close(reader)
        os.close(writer)
