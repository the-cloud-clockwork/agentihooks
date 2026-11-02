import os
import tempfile
from pathlib import Path

LOG_MAX_BYTES = 5 << 20


def rotate_if_full(path: Path, limit: int = LOG_MAX_BYTES) -> None:
    try:
        if path.stat().st_size >= limit:
            path.replace(path.with_name(path.name + ".1"))
    except FileNotFoundError:
        pass


def append_capped(path: Path, text: str, limit: int = LOG_MAX_BYTES) -> None:
    rotate_if_full(path, limit)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)


def atomic_write(path: Path, text: str) -> os.stat_result:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    written = os.stat(tmp)
    os.replace(tmp, path)
    return written
