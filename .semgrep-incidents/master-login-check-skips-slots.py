from collections.abc import Mapping
from pathlib import Path

from scripts.master_claude_login import _checked_or_moved, checked, prepare


def prepare_launch(environ: Mapping[str, str], cwd: Path) -> tuple[Path, str] | None:
    def current() -> tuple[Path, str] | None:
        target, found = prepare(environ, cwd)
        # ruleid: master-login-check-skips-slots
        if not checked(target, environ, found):
            return None
        return target, found

    return current()


def prepare_launch_moved(environ: Mapping[str, str], cwd: Path) -> tuple[Path, str] | None:
    def current() -> tuple[Path, str] | None:
        target, found = prepare(environ, cwd)
        # ok: master-login-check-skips-slots
        usable = _checked_or_moved(environ, cwd, target, found)
        if usable is None:
            return None
        return usable

    return current()
