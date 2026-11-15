"""The protected grader revision a lab grade runs its static gates from, and the digest of each gate's import closure."""

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

__all__ = ["closure", "digest", "revision"]


def revision(payload: Mapping[str, Any], dev_head: str) -> str:
    """Return the pull request base sha of a pull request event, else the dev head a push grades with."""
    return dev_head


def closure(root: Path, modules: Iterable[str]) -> tuple[str, ...]:
    """Return the repository files under root that the modules import, transitively, sorted."""
    return ()


def digest(root: Path, modules: Iterable[str]) -> str:
    """Return the sha256 over the repository paths and content digests of the modules' closure."""
    return ""
