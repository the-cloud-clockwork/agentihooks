import io
import os
import signal
import subprocess
import sys
import termios
import threading
import time

import pytest

from scripts import aicode_tokens, claude_login
from tests.codex_fakes import BAO_TOKEN, BAO_URL, FakeBao

TOKEN = "sk-ant-" + "oat01-" + "Ab1_-" * 20
CLAUDE_URL = "/v1/secret/data/agentihooks/claude-creds"
BAO_ENV = {"OPENBAO_URL": BAO_URL, "OPENBAO_TOKEN": BAO_TOKEN}
ASK_STORE = "Do you want to store them in OpenBao? [y/N] "
SKIP = "not a terminal: skipping OpenBao (pass --openbao to store the tokens there)"
NOTE = aicode_tokens.SLUG_NOTE.format(prefix="AH_CC_TOKEN_")
STEPS = (
    f"{NOTE}\n"
    "Step 1/3: sign in to the account in the browser\n"
    "use a private window for the account you want\n"
    "Step 2/3: writing the token block\n"
)


class Tty(io.StringIO):
    def isatty(self):
        return True


class Prompts:
    def __init__(self, answers=(), tty=True, hidden="typed-bao"):
        self.answers, self.prompts, self.is_tty, self.secret = list(answers), [], tty, hidden

    def ask(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0)

    def hidden(self, prompt):
        self.prompts.append(prompt)
        return self.secret

    def tty(self):
        return self.is_tty


def _runner(code=0, captured=None):
    calls = []

    def runner(argv):
        calls.append(argv)
        return code, b"\x1b[1mvisit\x1b[0m\r\n" + TOKEN.encode() + b"\r\n" if captured is None else captured

    return runner, calls


@pytest.fixture(autouse=True)
def _which(monkeypatch):
    monkeypatch.setattr(claude_login.shutil, "which", lambda name: f"/usr/bin/{name}")


def _login(args, environ=None, runner=None, console=None, bao=None):
    console = console or Prompts(tty=False)
    prompts = claude_login.Console(console.ask, console.hidden, console.tty)
    return claude_login.main(args, environ or {}, runner or _runner()[0], prompts, bao)


def _summary(name):
    rc = aicode_tokens.rc_file()
    return f"{name} written to {rc} ({len(TOKEN)} chars). run: source {rc}\n"


@pytest.mark.parametrize(
    "captured, expected",
    [
        (b"\x1b]8;;https://claude.ai\x07link\x1b]8;;\x07 \x1b[32m" + TOKEN.encode() + b"\x1b[0m", TOKEN),
        ((TOKEN[:60] + "\r\n  " + TOKEN[60:]).encode(), TOKEN[:60]),
        (b"\xff" + b"sk-ant-oat01-" + b"a" * 20 + b" " + TOKEN.encode(), TOKEN),
        (b"no token here", ""),
    ],
)
def test_the_token_is_the_longest_match_after_escapes_are_stripped(captured, expected):
    assert claude_login.extract(captured) == expected


def _pipe(data=b"", close=False):
    read, write = os.pipe()
    os.write(write, data)
    if close:
        os.close(write)
    return read


@pytest.fixture
def deadline():
    def expired(signum, frame):
        raise TimeoutError("the pty relay hung")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(20)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, previous)


def test_the_child_runs_in_a_wide_controlling_terminal_and_its_output_passes_through(deadline):
    out_read, out_write = os.pipe()
    child = (
        "import os, sys, time; s = os.get_terminal_size(); open('/dev/tty').close(); "
        "print(s.columns, s.lines, flush=True); time.sleep(0.3); print('end'); sys.exit(3)"
    )
    terminal = claude_login.Terminal(_pipe(b"zzz"), out_write, False, 33)
    code, captured = claude_login.run([sys.executable, "-c", child], terminal)
    assert (code, captured) == (3, b"1000 33\r\nend\r\n")
    assert os.read(out_read, 1024) == captured


def test_on_a_terminal_keys_are_forwarded_in_raw_mode_and_the_mode_is_restored(deadline):
    seen = []
    stdin = _pipe(b"hello\n", close=True)
    out_read, out_write = os.pipe()
    terminal = claude_login.Terminal(
        stdin,
        out_write,
        True,
        24,
        lambda fd: seen.append(("get", fd)) or "saved",
        lambda *args: seen.append(args),
        lambda fd: seen.append(fd),
    )
    code, captured = claude_login.run([sys.executable, "-c", "print('got', input())"], terminal)
    assert code == 0 and b"got hello" in captured
    assert seen == [("get", stdin), stdin, (stdin, termios.TCSADRAIN, "saved")]


def _feed(*chunks):
    read, write = os.pipe()

    def send():
        for chunk in chunks:
            os.write(write, chunk)
            time.sleep(0.2)
        os.close(write)

    threading.Thread(target=send, daemon=True).start()
    return read


def _modes(seen, raw=lambda fd: None):
    return {"get_mode": lambda fd: "saved", "set_mode": lambda *args: seen.append(args), "raw": raw}


def test_keys_sent_in_pieces_and_output_written_a_byte_at_a_time_all_arrive(monkeypatch, deadline):
    monkeypatch.setattr(claude_login, "_write", lambda fd, data: os.write(fd, data[:1]))
    out_read, out_write = os.pipe()
    terminal = claude_login.Terminal(_feed(b"hel", b"lo\n"), out_write, True, 24, **_modes([]))
    code, captured = claude_login.run([sys.executable, "-c", "print('got', input())"], terminal)
    assert code == 0 and b"got hello" in captured
    assert os.read(out_read, 4096) == captured


def test_a_key_that_cannot_reach_the_child_stops_forwarding_and_output_still_arrives(monkeypatch, deadline):
    out_read, out_write = os.pipe()
    tried = []

    def write(fd, data):
        if fd != out_write:
            tried.append(data)
            raise OSError(5, "Input/output error")
        return os.write(fd, data)

    monkeypatch.setattr(claude_login, "_write", write)
    terminal = claude_login.Terminal(_feed(b"a", b"b"), out_write, True, 24, **_modes([]))
    child = "import time; time.sleep(0.8); print('done')"
    assert claude_login.run([sys.executable, "-c", child], terminal) == (0, b"done\r\n")
    assert tried == [b"a"]


def test_a_plain_end_of_file_from_the_child_side_ends_the_capture(deadline):
    out_read, out_write = os.pipe()
    terminal = claude_login.Terminal(_pipe(), out_write, False, 24)
    assert claude_login._pump(_pipe(b"tail", close=True), terminal, False) == b"tail"
    assert os.read(out_read, 64) == b"tail"


def test_output_that_cannot_be_shown_is_still_captured(deadline):
    unwritable, _ = os.pipe()
    terminal = claude_login.Terminal(_pipe(), unwritable, False, 24)
    assert claude_login.run([sys.executable, "-c", "print('x')"], terminal) == (0, b"x\r\n")


def test_the_terminal_mode_and_signal_handlers_are_restored_when_raw_mode_fails(deadline):
    seen = []
    before = signal.getsignal(signal.SIGTERM)

    def broken(fd):
        raise termios.error(25, "Inappropriate ioctl for device")

    terminal = claude_login.Terminal(_pipe(), os.pipe()[1], True, 24, **_modes(seen, broken))
    with pytest.raises(termios.error):
        claude_login.run([sys.executable, "-c", "print('x')"], terminal)
    assert seen == [(terminal.stdin, termios.TCSADRAIN, "saved")]
    assert signal.getsignal(signal.SIGTERM) is before


class Interrupted(Exception):
    pass


@pytest.fixture
def popens(monkeypatch):
    started = []
    real = subprocess.Popen

    def popen(*args, **kwargs):
        started.append(real(*args, **kwargs))
        return started[-1]

    monkeypatch.setattr(claude_login.subprocess, "Popen", popen)
    return started


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGHUP])
def test_a_terminating_signal_restores_the_terminal_and_kills_the_child(number, popens, deadline):
    def stray(signum, frame):
        raise Interrupted(signum)

    previous = signal.signal(number, stray)
    seen = []
    terminal = claude_login.Terminal(
        _pipe(), os.pipe()[1], True, 24, **_modes(seen, lambda fd: os.kill(os.getpid(), number))
    )
    try:
        with pytest.raises(SystemExit) as raised:
            claude_login.run([sys.executable, "-c", "import time; time.sleep(5)"], terminal)
        assert raised.value.code == 128 + number
        assert signal.getsignal(number) is stray
    finally:
        signal.signal(number, previous)
    assert seen == [(terminal.stdin, termios.TCSADRAIN, "saved")]
    assert popens[0].returncode == -signal.SIGKILL


def test_a_child_that_outlives_its_output_is_killed_after_the_wait(monkeypatch, deadline):
    monkeypatch.setattr(claude_login, "WAIT_S", 0.3)
    child = (
        "import os, signal, time; signal.signal(signal.SIGHUP, signal.SIG_IGN); os.write(1, b'tok'); "
        "null = os.open(os.devnull, os.O_RDWR); "
        "[os.dup2(null, fd) for fd in (0, 1, 2)]; time.sleep(5)"
    )
    terminal = claude_login.Terminal(_pipe(), os.pipe()[1], False, 24)
    assert claude_login.run([sys.executable, "-c", child], terminal) == (-signal.SIGKILL, b"tok")


def _no_window(*args):
    raise OSError(25, "Inappropriate ioctl for device")


@pytest.mark.parametrize("argv, window", [(["/nonexistent/claude"], None), ([sys.executable, "-c", ""], _no_window)])
def test_a_child_that_cannot_start_raises_and_leaks_no_descriptor(argv, window, monkeypatch):
    terminal = claude_login.Terminal(_pipe(), os.pipe()[1], False, 24)
    if window:
        monkeypatch.setattr(claude_login.fcntl, "ioctl", window)
    before = sorted(os.listdir("/proc/self/fd"))
    with pytest.raises(OSError):
        claude_login.run(argv, terminal)
    assert sorted(os.listdir("/proc/self/fd")) == before


def test_the_current_terminal_reads_the_process_streams(monkeypatch):
    class Stream:
        def __init__(self, fd):
            self.fd = fd

        def fileno(self):
            return self.fd

        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", Stream(7))
    monkeypatch.setattr(sys, "stdout", Stream(9))
    monkeypatch.setattr(claude_login.shutil, "get_terminal_size", lambda: os.terminal_size((80, 51)))
    assert claude_login.Terminal.current() == claude_login.Terminal(7, 9, True, 51)


def test_setup_token_runs_on_the_current_terminal(monkeypatch):
    seen = []
    monkeypatch.setattr(claude_login.Terminal, "current", classmethod(lambda cls: "terminal"))
    monkeypatch.setattr(claude_login, "run", lambda argv, terminal: seen.append((argv, terminal)) or (0, b"x"))
    assert claude_login.setup_token(["claude", "setup-token"]) == (0, b"x")
    assert seen == [(["claude", "setup-token"], "terminal")]


def test_a_login_writes_the_token_block_and_never_prints_the_token(capsys):
    runner, calls = _runner()
    assert _login(["team"], runner=runner) == 0
    assert calls == [["/usr/bin/claude", "setup-token"]]
    assert f"export AH_CC_TOKEN_team={TOKEN}\n" in aicode_tokens.rc_file().read_text()
    out = capsys.readouterr()
    assert out.out == STEPS + _summary("AH_CC_TOKEN_team") + SKIP + "\n"
    assert TOKEN not in out.out + out.err


def test_claude_runs_by_name_when_it_is_not_on_the_path(monkeypatch):
    monkeypatch.setattr(claude_login.shutil, "which", lambda name: None)
    runner, calls = _runner()
    assert _login(["team", "--no-openbao"], runner=runner) == 0
    assert calls == [["claude", "setup-token"]]


def test_an_email_slug_and_its_raw_form_cleanup():
    rc = aicode_tokens.rc_file()
    rc.write_text(f"{aicode_tokens.START}\nexport AH_CC_TOKEN_me@x.io=old\n{aicode_tokens.END}\n")
    bao = FakeBao({"AH_CC_TOKEN_me@x.io": "old", "OTHER": "o"}, path=CLAUDE_URL)
    assert _login(["me@x.io", "--openbao"], environ=BAO_ENV, bao=bao) == 0
    assert rc.read_text() == f"{aicode_tokens.START}\nexport AH_CC_TOKEN_meatx_io={TOKEN}\n{aicode_tokens.END}\n"
    assert bao.data == {"OTHER": "o", "AH_CC_TOKEN_meatx_io": TOKEN}


def test_without_a_slug_a_terminal_is_asked_for_one(capsys):
    console = Prompts(["nestor.colt@gmail.com", "n"])
    assert _login([], console=console) == 0
    assert console.prompts == ["Slug (the email of the target account): ", ASK_STORE]
    assert "export AH_CC_TOKEN_nestor_coltatgmail_com=" in aicode_tokens.rc_file().read_text()


def test_without_a_slug_off_a_terminal_the_usage_is_shown_and_nothing_runs(capsys):
    runner, calls = _runner()
    assert _login([], runner=runner) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("usage: agentihooks claude login")
    assert err.endswith("agentihooks claude login: a slug is required off a terminal\n")
    assert not calls


def test_the_default_console_reads_the_real_terminal(capsys):
    assert claude_login.main([], {}, _runner()[0]) == 2


def test_a_bad_slug_is_refused_before_claude_runs(capsys):
    runner, calls = _runner()
    assert _login(["_x"], runner=runner) == 2
    assert capsys.readouterr().err == f"agentihooks claude login: {aicode_tokens.SLUG_RULE}: '_x'\n"
    assert not calls


@pytest.mark.parametrize("code, captured, expected", [(5, None, 5), (0, b"signed in, no token", 1)])
def test_no_token_writes_nothing_and_says_so(code, captured, expected, capsys):
    assert _login(["team"], runner=_runner(code, captured)[0]) == expected
    assert capsys.readouterr().err == (
        f"agentihooks claude login: no token found (claude setup-token exited {code}); nothing written\n"
    )
    assert not aicode_tokens.rc_file().exists()


def test_a_claude_that_cannot_start_is_named_and_nothing_is_written(capsys):
    def runner(argv):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    assert _login(["team"], runner=runner) == 1
    assert capsys.readouterr().err == (
        "agentihooks claude login: cannot run /usr/bin/claude: No such file or directory\n"
    )
    assert not aicode_tokens.rc_file().exists()


def test_the_steps_reach_the_screen_before_setup_token_runs(monkeypatch):
    class Recorder(io.StringIO):
        flushed = ""

        def flush(self):
            self.flushed = self.getvalue()

    out = Recorder()
    monkeypatch.setattr(sys, "stdout", out)
    seen = []

    def runner(argv):
        seen.append(out.flushed)
        return 0, TOKEN.encode()

    assert _login(["team", "--no-openbao"], runner=runner) == 0
    assert seen == [STEPS.removesuffix("Step 2/3: writing the token block\n")]


def test_openbao_stores_the_token_under_the_claude_secret(capsys):
    bao = FakeBao(path=CLAUDE_URL)
    assert _login(["team", "--openbao"], environ=BAO_ENV, bao=bao) == 0
    assert bao.data == {"AH_CC_TOKEN_team": TOKEN}
    assert capsys.readouterr().out == STEPS + _summary("AH_CC_TOKEN_team") + (
        "Step 3/3: storing it in OpenBao\n"
        "OpenBao secret/agentihooks/claude-creds: wrote AH_CC_TOKEN_team (1 keys total)\n"
    )


def test_openbao_refused_off_a_terminal_exits_one(capsys):
    assert _login(["team", "--openbao"]) == 1
    assert capsys.readouterr().err == (
        "agentihooks claude login: --openbao needs OPENBAO_URL and OPENBAO_TOKEN off a terminal\n"
    )


def test_a_terminal_sees_blue_steps_a_yellow_warning_and_a_red_error(monkeypatch):
    out, err = Tty(), Tty()
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    assert _login(["team"], runner=_runner(5)[0]) == 5
    assert out.getvalue() == (
        f"\x1b[34m{NOTE}\x1b[0m\n"
        "\x1b[34mStep 1/3: sign in to the account in the browser\x1b[0m\n"
        "\x1b[33muse a private window for the account you want\x1b[0m\n"
    )
    assert err.getvalue() == (
        "\x1b[31magentihooks claude login: no token found (claude setup-token exited 5); nothing written\x1b[0m\n"
    )


@pytest.mark.parametrize("out, err, painted", [(Tty(), io.StringIO(), True), (io.StringIO(), Tty(), False)])
def test_errors_are_coloured_only_when_stdout_is_a_terminal(out, err, painted, monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    assert _login(["_x"]) == 2
    message = f"agentihooks claude login: {aicode_tokens.SLUG_RULE}: '_x'"
    assert err.getvalue() == (f"\x1b[31m{message}\x1b[0m\n" if painted else f"{message}\n")


def test_no_openbao_on_a_terminal_asks_nothing(capsys):
    console = Prompts(tty=True)
    assert _login(["team", "--no-openbao"], console=console) == 0
    assert console.prompts == []
    assert capsys.readouterr().out == STEPS + _summary("AH_CC_TOKEN_team")


def test_login_is_dispatched_and_everything_else_falls_back(monkeypatch):
    seen = []
    monkeypatch.setattr(claude_login, "main", lambda args: seen.append(("login", args)) or 4)
    with pytest.raises(SystemExit) as raised:
        claude_login.dispatch(["login", "team"], lambda rest: seen.append(("claude", rest)))
    assert raised.value.code == 4
    for args in (["-p", "hi"], ["--resume", "abc"], []):
        claude_login.dispatch(args, lambda rest: seen.append(("claude", rest)))
    assert seen == [("login", ["team"]), ("claude", ["-p", "hi"]), ("claude", ["--resume", "abc"]), ("claude", [])]


def test_agentihooks_claude_checks_for_login_before_routing(monkeypatch):
    from scripts import install

    seen = []
    monkeypatch.setattr(claude_login, "main", lambda args: seen.append(("login", args)) or 6)
    monkeypatch.setattr(install, "cmd_claude", lambda args: seen.append(("claude", args)))
    monkeypatch.setattr(sys, "argv", ["agentihooks", "claude", "login", "team"])
    with pytest.raises(SystemExit) as raised:
        install.main()
    monkeypatch.setattr(sys, "argv", ["agentihooks", "claude", "-p", "hi"])
    install.main()
    assert raised.value.code == 6
    assert seen == [("login", ["team"]), ("claude", ["-p", "hi"])]
