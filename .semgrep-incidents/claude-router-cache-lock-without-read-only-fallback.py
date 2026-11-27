import errno
import fcntl
from collections.abc import Generator
from pathlib import Path
from typing import TextIO


def lock_without_fallback(path: Path) -> Generator[None]:
    # ruleid: claude-router-cache-lock-without-read-only-fallback
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def open_swallowing_every_error(path: Path) -> TextIO | None:
    try:
        # ruleid: claude-router-cache-lock-without-read-only-fallback
        return path.open("a+", encoding="utf-8")
    except OSError:
        return None


def open_with_fallback(path: Path) -> TextIO | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # ok: claude-router-cache-lock-without-read-only-fallback
        return path.open("a+", encoding="utf-8")
    except OSError as error:
        if error.errno != errno.EROFS:
            raise
        return None
