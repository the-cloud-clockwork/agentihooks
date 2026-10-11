import io
import os
import sys
import termios

import pytest

from scripts import aicode_tokens, claude_login
from tests.codex_fakes import BAO_TOKEN, BAO_URL, FakeBao

TOKEN = "sk-ant-" + "oat01-" + "Ab1_-" * 20
CLAUDE_URL = "/v1/secret/data/agentihooks/claude-creds"
BAO_ENV = {"OPENBAO_URL": BAO_URL, "OPENBAO_TOKEN": BAO_TOKEN}
ASK_STORE = "Do you want to store them in OpenBao? [y/N] "
SKIP = "not a terminal: skipping OpenBao (pass --openbao to store the tokens there)"
STEPS = (
    f"{aicode_tokens.SLUG_NOTE}\n"
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


def test_the_child_runs_in_a_wide_controlling_terminal_and_its_output_passes_through():
    out_read, out_write = os.pipe()
    child = (
        "import os, sys; s = os.get_terminal_size(); open('/dev/tty').close(); print(s.columns, s.lines); sys.exit(3)"
    )
    terminal = claude_login.Terminal(_pipe(b"zzz"), out_write, False, 33)
    code, captured = claude_login.run([sys.executable, "-c", child], terminal)
    assert (code, captured) == (3, b"1000 33\r\n")
    assert os.read(out_read, 1024) == captured


def test_on_a_terminal_keys_are_forwarded_in_raw_mode_and_the_mode_is_restored():
    seen = []
    stdin = _pipe(b"hello\n", close=True)
    out_read, out_write = os.pipe()
    terminal = claude_login.Terminal(
        stdin, out_write, True, 24, lambda fd: "saved", lambda *args: seen.append(args), lambda fd: seen.append(fd)
    )
    code, captured = claude_login.run([sys.executable, "-c", "print('got', input())"], terminal)
    assert code == 0 and b"got hello" in captured
    assert seen == [stdin, (stdin, termios.TCSADRAIN, "saved")]


def test_the_terminal_mode_is_restored_when_relaying_fails():
    seen = []
    unwritable, _ = os.pipe()
    terminal = claude_login.Terminal(
        _pipe(), unwritable, True, 24, lambda fd: "saved", lambda *args: seen.append(args), lambda fd: None
    )
    with pytest.raises(OSError):
        claude_login.run([sys.executable, "-c", "print('x')"], terminal)
    assert seen[-1][1:] == (termios.TCSADRAIN, "saved")


def test_a_missing_binary_exits_127_with_nothing_captured():
    terminal = claude_login.Terminal(_pipe(), os.pipe()[1], False, 24)
    assert claude_login.run(["/nonexistent/claude"], terminal) == (127, b"")


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
    assert rc.read_text() == f"{aicode_tokens.START}\nexport AH_CC_TOKEN_me_x_io={TOKEN}\n{aicode_tokens.END}\n"
    assert bao.data == {"OTHER": "o", "AH_CC_TOKEN_me_x_io": TOKEN}


def test_without_a_slug_a_terminal_is_asked_for_one(capsys):
    console = Prompts(["nestor.colt@gmail.com", "n"])
    assert _login([], console=console) == 0
    assert console.prompts == ["Slug (the email of the target account): ", ASK_STORE]
    assert "export AH_CC_TOKEN_nestor_colt_gmail_com=" in aicode_tokens.rc_file().read_text()


def test_without_a_slug_off_a_terminal_the_usage_is_shown_and_nothing_runs(capsys):
    runner, calls = _runner()
    assert _login([], runner=runner) == 2
    err = capsys.readouterr().err
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
        f"\x1b[34m{aicode_tokens.SLUG_NOTE}\x1b[0m\n"
        "\x1b[34mStep 1/3: sign in to the account in the browser\x1b[0m\n"
        "\x1b[33muse a private window for the account you want\x1b[0m\n"
    )
    assert err.getvalue() == (
        "\x1b[31magentihooks claude login: no token found (claude setup-token exited 5); nothing written\x1b[0m\n"
    )


def test_login_is_intercepted_and_everything_else_passes_through(monkeypatch):
    seen = []
    monkeypatch.setattr(claude_login, "main", lambda args: seen.append(args) or 4)
    with pytest.raises(SystemExit) as raised:
        claude_login.intercept(["login", "team"])
    assert (raised.value.code, seen) == (4, [["team"]])
    assert claude_login.intercept(["-p", "hi"]) is None
    assert claude_login.intercept(["--resume", "abc"]) is None
    assert claude_login.intercept([]) is None


def test_agentihooks_claude_checks_for_login_before_routing(monkeypatch):
    from scripts import install

    def intercept(args):
        raise RuntimeError(f"intercepted {args}")

    monkeypatch.setattr(claude_login, "intercept", intercept)
    with pytest.raises(RuntimeError, match=r"intercepted \['-p', 'hi'\]"):
        install.cmd_claude(["-p", "hi"])
