"""The repository files a pytest worker reads, recorded by an audit hook the lab's sitecustomize installs.

The test executor loads this file by path in every process of a batch run, so it imports only the standard library.
"""

import inspect
import os
import sys
from types import FrameType
from typing import Any, BinaryIO

__all__ = ["OPENED", "TREE", "WORKER", "Capture", "install", "loading"]

TREE = "CI_LAB_TREE"
OPENED = "CI_LAB_OPENED"
WORKER = "PYTEST_XDIST_WORKER"
HELPERS = (f"{os.sep}pathlib.py", f"{os.sep}tokenize.py")
LOADERS = (
    "<frozen importlib",
    f"{os.sep}importlib{os.sep}",
    f"{os.sep}_pytest{os.sep}assertion{os.sep}",
    "linecache.py",
)
WRITES = frozenset("wax")


def loading(frame: FrameType | None) -> bool:
    """Return whether the code that opened a file, past pathlib and tokenize helpers, is an import, assertion
    rewriting or a traceback's source lookup."""
    while frame is not None and frame.f_code.co_filename.endswith(HELPERS):
        frame = frame.f_back
    return frame is not None and any(loader in frame.f_code.co_filename for loader in LOADERS)


def _read(path: object, mode: object) -> str | None:
    """Return the absolute path an open event reads, None for a write, a file descriptor or a path the process cannot
    resolve."""
    if not isinstance(path, (str, bytes, os.PathLike)) or (isinstance(mode, str) and WRITES & set(mode)):
        return None
    try:
        return os.path.abspath(os.fsdecode(path))  # pyright: ignore[reportUnknownArgumentType]
    except OSError:
        return None


class Capture:
    """Appends each file under the absolute root that a pytest worker reads, once, to the log relative to root."""

    def __init__(self, root: str, log: BinaryIO) -> None:
        self.root = os.path.join(root, "")
        self.log = log
        self.seen: set[str] = set()

    def audit(self, args: tuple[Any, ...], frame: FrameType | None) -> None:
        """Record the file an open event's arguments read under root, when code other than an import opened it."""
        if not os.environ.get(WORKER):
            return
        path = _read(args[0], args[1])
        if path is None or not path.startswith(self.root) or path in self.seen or loading(frame):
            return
        self.seen.add(path)
        self.log.write(os.fsencode(path[len(self.root) :]) + b"\n")
        self.log.flush()


def install() -> None:
    """Hook a capture into this process when the environment names the tree and the folder for its log.

    The hook looks up its caller's frame only for an open event, since the lookup raises an audit event of its own.
    """
    root, folder = os.environ.get(TREE), os.environ.get(OPENED)
    if not root or not folder:
        return
    capture = Capture(root, open(os.path.join(folder, f"{os.getpid()}.txt"), "ab"))

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event == "open":
            frame = inspect.currentframe()
            capture.audit(args, frame and frame.f_back)

    sys.addaudithook(audit)
