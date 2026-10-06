import fcntl
import os
import sys
from pathlib import Path

import pytest

WRITE_ROOT = None
PROTECTED_PATHS = ()


def _mutation_paths(event, args):
    if event == "open":
        return [(args[0], -1)] if args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND) else []
    if event in {"os.rename", "os.link"}:
        return [(args[0], args[2]), (args[1], args[3])]
    if event == "os.symlink":
        return [(args[1], args[2])]
    if event in {"os.remove", "os.rmdir"}:
        return [(args[0], args[1])]
    if event in {"os.mkdir", "os.chmod", "os.chown", "os.utime"}:
        return [(args[0], args[-1])]
    if event == "os.truncate":
        return [(args[0], -1)]
    return []


def _installer_on_stack():
    frame = sys._getframe(1)
    while frame is not None:
        name = frame.f_globals.get("__name__", "")
        if name in {"importlib._bootstrap_external", "_frozen_importlib_external"}:
            return False
        filename = frame.f_code.co_filename.replace("\\", "/")
        if filename.endswith("/scripts/install.py") or "/scripts/targets/" in filename:
            return True
        frame = frame.f_back
    return False


def _descriptor_path(fd):
    if sys.platform == "darwin":
        return Path(os.fsdecode(fcntl.fcntl(fd, 50, bytes(1024)).split(b"\0", 1)[0]))
    return Path(os.readlink(f"/proc/self/fd/{fd}"))


def refuse_unsafe_installer_write(event: str, args: tuple) -> None:
    if WRITE_ROOT is None:
        return
    for operand, dir_fd in _mutation_paths(event, args):
        if isinstance(operand, int):
            continue
        path = Path(os.fsdecode(operand))
        if not path.is_absolute() and dir_fd not in {-1, None}:
            path = _descriptor_path(dir_fd) / path
        if event in {"os.remove", "os.rmdir", "os.rename", "os.link", "os.symlink"}:
            path = path.parent.resolve() / path.name
        else:
            path = path.resolve()
        protected = any(path.is_relative_to(root) for root in PROTECTED_PATHS)
        if not path.is_relative_to(WRITE_ROOT) and (protected or _installer_on_stack()):
            pytest.fail(f"refusing installer write outside the test directory: {path}")


sys.addaudithook(refuse_unsafe_installer_write)
