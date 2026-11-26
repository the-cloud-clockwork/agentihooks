import os
import sys
from collections.abc import Callable


def _claim() -> str | None:
    return None


# ruleid: platform-chosen-at-import
GATES = () if sys.platform == "win32" else (("claim watch", _claim),)

# ruleid: platform-chosen-at-import
WINDOWS = sys.platform == "win32"

# ruleid: platform-chosen-at-import
ACTIVE: bool = sys.platform != "win32"

# ruleid: platform-chosen-at-import
SHELL = "cmd" if sys.platform.startswith("win") else "bash"


class Settings:
    # ruleid: platform-chosen-at-import
    skip_gates = sys.platform == "win32"


# ok: platform-chosen-at-import
ALL_GATES: tuple[tuple[str, Callable[[], str | None]], ...] = (("claim watch", _claim),)

# ok: platform-chosen-at-import
SEPARATOR = os.sep


def pretool() -> str | None:
    # ok: platform-chosen-at-import
    skip = sys.platform == "win32"
    if skip:
        return None
    for _label, gate in ALL_GATES:
        block = gate()
        if block:
            return block
    return None


async def apretool() -> str | None:
    # ok: platform-chosen-at-import
    skip = sys.platform == "win32"
    return None if skip else pretool()


class Runner:
    def windows(self) -> bool:
        # ok: platform-chosen-at-import
        found = sys.platform == "win32"
        return found
