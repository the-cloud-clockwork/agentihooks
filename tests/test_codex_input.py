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
    assert guarded == [sys.executable, "-P", "-m", "scripts.codex_input", "--", *plain]
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
        (b"\x1b[@", b"\x1b[@"),
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


def _probe_state(pid):
    lines = []
    pending = [pid]
    while pending:
        current = pending.pop()
        try:
            status = open(f"/proc/{current}/status").read()
            wchan = open(f"/proc/{current}/wchan").read()
            children = []
            for task in os.listdir(f"/proc/{current}/task"):
                children += open(f"/proc/{current}/task/{task}/children").read().split()
        except OSError as exc:
            lines.append(f"{current}: {exc}")
            continue
        keep = [
            line
            for line in status.splitlines()
            if line.split(":")[0]
            in {"Name", "State", "SigQ", "SigPnd", "ShdPnd", "SigBlk", "SigIgn", "SigCgt", "PPid", "NSpgid"}
        ]
        lines.append(f"{current} wchan={wchan} " + " | ".join(keep))
        pending += [int(child) for child in children]
    return "\n".join(lines)


@pytest.mark.parametrize("attempt", range(400))
def test_probe_signal_interrupt(attempt):
    lines = [
        f"worker SigIgn/SigBlk: {[line for line in open('/proc/self/status').read().splitlines() if line.startswith(('SigIgn', 'SigBlk'))]}"
    ]
    child = "import time; print('READY',flush=True); time.sleep(5)"
    with _relay(child) as (process, master, slave, saved):
        _read_until(master, b"READY")
        lines.append("before kill:\n" + _probe_state(process.pid))
        os.kill(process.pid, signal.SIGINT)
        try:
            code = process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            lines.append("after 2s:\n" + _probe_state(process.pid))
            output = b""
            while select.select([master], [], [], 0.1)[0]:
                output += os.read(master, 65536)
            lines.append(f"output: {output!r}")
            pytest.fail("\n".join(lines))
        assert code == 130, "\n".join(lines)


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


def test_fragmented_ss3_waits_for_its_final_key_byte():
    framer = codex_input.InputFramer()
    assert framer.feed(b"\x1bO") == b""
    assert framer.feed(b"A") == b"\x1bOA"


def test_interrupt_before_any_escape_is_preserved():
    framer = codex_input.InputFramer()
    assert framer.feed(b"\x03text") == b"\x03text"


def test_delete_byte_inside_csi_does_not_finish_it():
    framer = codex_input.InputFramer()
    assert framer.feed(b"\x1b[\x7f") == b""
    assert framer.feed(b"~") == b"\x1b[\x7f~"


@pytest.mark.parametrize("ending", [b"", OSError(5, "terminal closed")])
def test_relay_forwards_filtered_input_output_and_escape_timeout(monkeypatch, ending):
    from unittest.mock import Mock

    ready = iter([[0], [0], [0], [9], [0], [], [0], [9]])
    reads = iter([b"\x1b[<35;100;30Mtext", b"\x1b", b"[3~", b"child output", b"\x1b", b"", ending])
    descriptors = iter([0, 0, 0, 9, 0, 0, 9])
    clocks = iter([0.0, 0.1, 0.4, 0.5, 0.6, 1.6, 1.7, 1.8])
    rounds = iter(range(8))
    writes = []
    now = [0.0]

    def select_readers(readers, writable, exceptional, timeout):
        assert writable == exceptional == []
        assert timeout == 0.05
        assert readers == ([9] if next(rounds) == 7 else [9, 0])
        now[0] = next(clocks)
        return next(ready), [], []

    def read(fd, limit):
        assert limit == 65536
        assert fd == next(descriptors)
        result = next(reads)
        if isinstance(result, OSError):
            raise result
        return result

    monkeypatch.setattr(codex_input.select, "select", select_readers)
    monkeypatch.setattr(codex_input.os, "read", read)
    monkeypatch.setattr(codex_input.time, "monotonic", lambda: now[0])
    write = Mock(side_effect=lambda fd, data: writes.append((fd, data)) if data else None)
    monkeypatch.setattr(codex_input, "write_all", write)
    codex_input.pump(9)
    assert writes == [(9, b"text"), (9, b"\x1b[3~"), (1, b"child output"), (9, b"\x1b")]


@pytest.mark.parametrize(("fd", "error"), [(0, 5), (9, 9)])
def test_relay_propagates_unexpected_read_errors(monkeypatch, fd, error):
    monkeypatch.setattr(codex_input.select, "select", lambda *args: ([fd], [], []))
    monkeypatch.setattr(codex_input, "write_all", lambda *args: None)

    def read(*args):
        raise OSError(error, "read failed")

    monkeypatch.setattr(codex_input.os, "read", read)
    with pytest.raises(OSError) as caught:
        codex_input.pump(9)
    assert caught.value.errno == error


@pytest.mark.parametrize(("status", "expected"), [(0, 0), (23 << 8, 23), (signal.SIGTERM, 143)])
def test_terminal_relay_restores_state_and_forwards_controls(monkeypatch, status, expected):
    from unittest.mock import Mock, call

    saved = [1, 2, 3]
    handlers = {}
    previous = {}
    tty_check = Mock(return_value=True)
    getattrs = Mock(return_value=saved)
    setattrs = Mock()
    getsize = Mock(return_value=(55, 160))
    setsize = Mock()
    raw = Mock()
    close = Mock()
    kill = Mock()
    wait = Mock(return_value=(42, status))

    def install(number, handler):
        old = handlers.get(number, object())
        previous.setdefault(number, old)
        handlers[number] = handler
        return old

    def pump(master):
        assert master == 9
        handlers[signal.SIGWINCH](signal.SIGWINCH, None)
        for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            handlers[number](number, None)

    monkeypatch.setattr(codex_input.os, "isatty", tty_check)
    monkeypatch.setattr(codex_input.termios, "tcgetattr", getattrs)
    monkeypatch.setattr(codex_input.termios, "tcsetattr", setattrs)
    monkeypatch.setattr(codex_input.termios, "tcgetwinsize", getsize)
    monkeypatch.setattr(codex_input.termios, "tcsetwinsize", setsize)
    monkeypatch.setattr(codex_input.pty, "fork", lambda: (42, 9))
    monkeypatch.setattr(codex_input.signal, "signal", install)
    monkeypatch.setattr(codex_input.tty, "setraw", raw)
    monkeypatch.setattr(codex_input, "pump", pump)
    monkeypatch.setattr(codex_input.os, "close", close)
    monkeypatch.setattr(codex_input.os, "killpg", kill)
    monkeypatch.setattr(codex_input.os, "waitpid", wait)
    assert codex_input.run(["codex", "resume", "conversation"]) == expected
    tty_check.assert_called_once_with(0)
    getattrs.assert_called_once_with(0)
    assert getsize.call_args_list == [call(0), call(0)]
    setsize.assert_called_once_with(9, (55, 160))
    raw.assert_called_once_with(0)
    setattrs.assert_called_once_with(0, termios.TCSADRAIN, saved)
    close.assert_called_once_with(9)
    wait.assert_called_once_with(42, 0)
    assert kill.call_args_list == [call(42, number) for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)]
    assert handlers == previous


@pytest.mark.parametrize("terminal", [False, True])
def test_exec_preserves_the_routed_argument_vector(monkeypatch, terminal):
    from unittest.mock import Mock

    class Executed(Exception):
        pass

    argv = ["codex", "resume", "conversation"]
    execvp = Mock(side_effect=Executed)
    saved = [1, 2, 3]
    setattrs = Mock()
    setsize = Mock()
    monkeypatch.setattr(codex_input.os, "isatty", lambda fd: terminal if fd == 0 else not terminal)
    monkeypatch.setattr(codex_input.os, "execvp", execvp)
    monkeypatch.setattr(codex_input.termios, "tcgetattr", lambda fd: saved if fd == 0 else None)
    monkeypatch.setattr(codex_input.termios, "tcgetwinsize", lambda fd: (55, 160) if fd == 0 else None)
    monkeypatch.setattr(codex_input.termios, "tcsetattr", setattrs)
    monkeypatch.setattr(codex_input.termios, "tcsetwinsize", setsize)
    monkeypatch.setattr(codex_input.pty, "fork", lambda: (0, 9))
    with pytest.raises(Executed):
        codex_input.run(argv)
    execvp.assert_called_once_with("codex", argv)
    if terminal:
        setattrs.assert_called_once_with(0, termios.TCSANOW, saved)
        setsize.assert_called_once_with(0, (55, 160))
    else:
        setattrs.assert_not_called()
        setsize.assert_not_called()


@pytest.mark.parametrize("separator", [[], ["--"]])
def test_guard_cli_preserves_all_command_arguments(monkeypatch, separator):
    from unittest.mock import Mock

    argv = ["codex", "resume", "conversation"]
    run = Mock(return_value=23)
    monkeypatch.setattr(codex_input.sys, "argv", ["guard", *separator, *argv])
    monkeypatch.setattr(codex_input, "run", run)
    assert codex_input.main() == 23
    run.assert_called_once_with(argv)
