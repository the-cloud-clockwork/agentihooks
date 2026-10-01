import os
from pathlib import Path

from hooks.config import LOG_BACKUPS, LOG_MAX_BYTES


def rotate_if_full(path: Path, limit: int = LOG_MAX_BYTES, backups: int = LOG_BACKUPS) -> None:
    try:
        if path.stat().st_size < limit:
            return
        for index in range(backups - 1, 0, -1):
            older = path.with_name(f"{path.name}.{index}")
            if older.exists():
                os.replace(older, path.with_name(f"{path.name}.{index + 1}"))
        if backups > 0:
            os.replace(path, path.with_name(f"{path.name}.1"))
        else:
            path.unlink()
    except OSError:
        pass


def append_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rotate_if_full(path)
    with open(path, "a") as handle:
        handle.write(text)
