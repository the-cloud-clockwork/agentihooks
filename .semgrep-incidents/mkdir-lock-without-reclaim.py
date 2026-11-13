import time
from pathlib import Path

from scripts import claude_trust


def _reclaim(lock: Path) -> bool:
    return not lock.exists()


def held_forever(lock: Path, deadline: float) -> None:
    # ruleid: mkdir-lock-without-reclaim
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise OSError(f"{lock} is held") from None
            time.sleep(0.1)
    try:
        pass
    finally:
        lock.rmdir()


def reclaimed(lock: Path, deadline: float) -> None:
    # ok: mkdir-lock-without-reclaim
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            if _reclaim(lock):
                continue
            if time.monotonic() >= deadline:
                raise OSError(f"{lock} is held") from None
            time.sleep(0.1)
    try:
        pass
    finally:
        lock.rmdir()


def reclaimed_through_module(lock: Path, deadline: float) -> None:
    # ok: mkdir-lock-without-reclaim
    while True:
        try:
            lock.mkdir()
            break
        except FileExistsError:
            if claude_trust._reclaim(lock):
                continue
            if time.monotonic() >= deadline:
                raise OSError(f"{lock} is held") from None
            time.sleep(0.1)
    try:
        pass
    finally:
        lock.rmdir()
