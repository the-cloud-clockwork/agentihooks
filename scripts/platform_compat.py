"""Advisory locking and filesystem links that degrade on platforms missing fcntl or symlink privilege."""

from __future__ import annotations

import contextlib
import shutil
from collections.abc import Iterator
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


@contextlib.contextmanager
def file_lock(path: Path, *, blocking: bool = True) -> Iterator[None]:
    if fcntl is None:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(path, "w")  # noqa: SIM115
    try:
        flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, flags)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def make_link(item: Path, link: Path) -> None:
    try:
        link.symlink_to(item)
    except OSError:
        if item.is_dir():
            shutil.copytree(item, link)
        else:
            shutil.copy2(item, link)
