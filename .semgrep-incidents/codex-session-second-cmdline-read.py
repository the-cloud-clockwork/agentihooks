from pathlib import Path


def _cmdline(pid: int, proc: Path) -> list[str]:
    return (proc / str(pid) / "cmdline").read_text().split("\0")


def _is_codex(pid: int, proc: Path) -> bool:
    return bool(_cmdline(pid, proc))


def _codex_argv(pid: int, proc: Path) -> list[str]:
    argv = _cmdline(pid, proc)
    return argv if argv and Path(argv[0]).name == "codex" else []


def session_rereads_after_the_check(pid: int, proc: Path) -> bool:
    if not _is_codex(pid, proc):
        return False
    argv = _cmdline(pid, proc)
    # ruleid: codex-session-second-cmdline-read
    return "app-server" not in argv[0]


def execution_rereads_after_the_check(pid: int, proc: Path) -> str:
    if not _is_codex(pid, proc):
        return ""
    argv = _cmdline(pid, proc)
    if "--listen" not in argv:
        return ""
    # ruleid: codex-session-second-cmdline-read
    return argv[argv.index("--listen") + 1]


def session_from_one_read(pid: int, proc: Path) -> bool:
    argv = _codex_argv(pid, proc)
    if not argv:
        return False
    # ok: codex-session-second-cmdline-read
    return "app-server" not in argv[0]
