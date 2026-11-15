"""The repository files a pytest worker reads, recorded by an audit hook the lab's sitecustomize installs.

The test executor loads this file by path in every process of a batch run, so it imports only the standard library.
"""

from types import FrameType
from typing import Any, TextIO

__all__ = ["OPENED", "TREE", "WORKER", "Capture", "install", "loading"]

TREE = "CI_LAB_TREE"
OPENED = "CI_LAB_OPENED"
WORKER = "PYTEST_XDIST_WORKER"


def loading(frame: FrameType | None) -> bool:
    """Return whether the code that opened a file, past pathlib and tokenize helpers, is an import, assertion
    rewriting or a traceback's source lookup."""
    raise NotImplementedError


class Capture:
    """Appends each file under root that a pytest worker reads, once, to the log as a path relative to root."""

    def __init__(self, root: str, log: TextIO) -> None:
        self.root = root
        self.log = log
        self.seen: set[str] = set()

    def audit(self, event: str, args: tuple[Any, ...], frame: FrameType | None) -> None:
        """Record an open event that reads a file under root from code other than an import."""
        raise NotImplementedError


def install() -> None:
    """Hook a capture into this process when the environment names the tree and the folder for its log."""
    raise NotImplementedError
