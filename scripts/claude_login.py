import argparse
import fcntl
import functools
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import tty
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from hooks.context.account_sessions import TOKEN_PREFIX
from scripts import aicode_tokens, openbao_kv, openbao_step, term_colors
from scripts.openbao_step import Console, StoreRefused

PROG = "agentihooks claude login"
TOKEN = re.compile(r"sk-ant-oat01-[A-Za-z0-9_\-]{20,}")
ESCAPES = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
COLUMNS = 1000
PIXELS = (0, 0)
WINSIZE = "HHHH"
CHUNK = 65536
WAIT_S = 10
SIGNALS = (signal.SIGTERM, signal.SIGHUP)
ASK_SLUG = "Slug (the email of the target account): "
STEP_SIGN_IN = "Step 1/3: sign in to the account in the browser"
PRIVATE_WINDOW = "use a private window for the account you want"
STEP_BLOCK = "Step 2/3: writing the token block"
STEP_STORE = "Step 3/3: storing it in OpenBao"
NO_TOKEN = "no token found (claude setup-token exited {code}); nothing written"
NO_SLUG = "a slug is required off a terminal"
CANNOT_RUN = "cannot run {binary}: {reason}"
SUMMARY = "{name} written to {rc} ({length} chars). run: source {rc}"
_write = os.write


@dataclass(frozen=True)
class Terminal:
    stdin: int
    stdout: int
    tty: bool
    rows: int
    get_mode: Callable = termios.tcgetattr
    set_mode: Callable = termios.tcsetattr
    raw: Callable = tty.setraw

    @classmethod
    def current(cls) -> "Terminal":
        return cls(sys.stdin.fileno(), sys.stdout.fileno(), sys.stdin.isatty(), shutil.get_terminal_size().lines)


def extract(captured: bytes) -> str:
    return max(TOKEN.findall(ESCAPES.sub("", captured.decode(errors="replace"))), key=len, default="")


def _write_all(fd: int, data: bytes) -> bool:
    try:
        while data:
            data = data[_write(fd, data) :]
    except OSError:
        return False
    return True


def _pump(master: int, terminal: Terminal, forward: bool) -> bytes:
    captured = bytearray()
    sources = [master, terminal.stdin] if forward else [master]
    while True:
        ready = select.select(sources, [], [])[0]
        if terminal.stdin in ready:
            data = os.read(terminal.stdin, CHUNK)
            if not (data and _write_all(master, data)):
                sources = [master]
        if master in ready:
            try:
                data = os.read(master, CHUNK)
            except OSError:
                return bytes(captured)
            if not data:
                return bytes(captured)
            captured += data
            _write_all(terminal.stdout, data)


def _interrupt(signum: int, frame) -> None:
    raise SystemExit(128 + signum)


def _relay(master: int, terminal: Terminal) -> bytes:
    saved = terminal.get_mode(terminal.stdin) if terminal.tty else None
    previous = {number: signal.signal(number, _interrupt) for number in SIGNALS}
    try:
        if saved is not None:
            terminal.raw(terminal.stdin)
        return _pump(master, terminal, saved is not None)
    finally:
        if saved is not None:
            terminal.set_mode(terminal.stdin, termios.TCSADRAIN, saved)
        for number, handler in previous.items():
            signal.signal(number, handler)


def _reap(child: subprocess.Popen) -> int:
    try:
        return child.wait(timeout=WAIT_S)
    except subprocess.TimeoutExpired:
        child.kill()
        return child.wait()


def run(argv: list[str], terminal: Terminal) -> tuple[int, bytes]:
    """The child's window is COLUMNS wide so the token prints on one line instead of being hard-wrapped."""
    master, slave = pty.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack(WINSIZE, terminal.rows, COLUMNS, *PIXELS))
        child = subprocess.Popen(argv, preexec_fn=functools.partial(os.login_tty, slave))
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    try:
        captured = _relay(master, terminal)
    except BaseException:
        child.kill()
        raise
    finally:
        os.close(master)
        code = _reap(child)
    return code, captured


def setup_token(argv: list[str]) -> tuple[int, bytes]:
    return run(argv, Terminal.current())


def _fail(message: str) -> None:
    print(term_colors.red(f"{PROG}: {message}"), file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROG)
    parser.add_argument("slug", nargs="?")
    store = parser.add_mutually_exclusive_group()
    store.add_argument("--openbao", action="store_true", default=None)
    store.add_argument("--no-openbao", dest="openbao", action="store_false")
    return parser


def _raw_slug(given: str | None, console: Console, parser: argparse.ArgumentParser) -> str | None:
    if given is None and not console.tty():
        parser.print_usage(sys.stderr)
        _fail(NO_SLUG)
        return None
    print(term_colors.blue(aicode_tokens.SLUG_NOTE.format(prefix=TOKEN_PREFIX)))
    return given if given is not None else console.ask(term_colors.yellow(ASK_SLUG)).strip()


def _capture(runner: Callable) -> tuple[int, str]:
    print(term_colors.blue(STEP_SIGN_IN))
    print(term_colors.yellow(PRIVATE_WINDOW))
    sys.stdout.flush()
    argv = [shutil.which("claude") or "claude", "setup-token"]
    try:
        code, captured = runner(argv)
    except OSError as exc:
        _fail(CANNOT_RUN.format(binary=argv[0], reason=exc.strerror or exc))
        return 1, ""
    token = extract(captured)
    failed = code or not token
    if failed:
        _fail(NO_TOKEN.format(code=code))
    return (code or 1) if failed else 0, token


def main(
    args: list[str],
    environ: Mapping[str, str] | None = None,
    runner: Callable = setup_token,
    console: Console | None = None,
    opener: Callable | None = None,
) -> int:
    environ = os.environ if environ is None else environ
    console = console or Console()
    parser = _parser()
    options = parser.parse_args(args)
    raw = _raw_slug(options.slug, console, parser)
    if raw is None:
        return 2
    try:
        slug = aicode_tokens.slug(raw)
    except aicode_tokens.SlugError as exc:
        _fail(str(exc))
        return 2
    code, token = _capture(runner)
    if code:
        return code
    name = f"{TOKEN_PREFIX}{slug}"
    stale = () if raw == slug else (f"{TOKEN_PREFIX}{raw}",)
    rc = aicode_tokens.rc_file()
    print(term_colors.blue(STEP_BLOCK))
    aicode_tokens.write_rc(rc, {name: token}, stale)
    print(SUMMARY.format(name=name, rc=rc, length=len(token)))
    try:
        bao = openbao_step.target(options.openbao, environ, console, opener)
    except StoreRefused as exc:
        _fail(str(exc))
        return 1
    if bao is None:
        return 0
    print(term_colors.blue(STEP_STORE))
    return openbao_step.store(bao, openbao_kv.CLAUDE_CREDS, {name: token}, stale)


def dispatch(args: list[str], fallback: Callable[[list[str]], object]) -> None:
    if args[:1] == ["login"]:
        raise SystemExit(main(args[1:]))
    fallback(args)
