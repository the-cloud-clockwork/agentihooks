"""Static gates discovered from the workflow tree: python -m scripts.ci_lab.static_gates [--root PATH] [--grader PATH]."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = ["WHOLE_TREE", "StaticGate", "discover", "main"]

WHOLE_TREE = (".",)


@dataclass(frozen=True)
class StaticGate:
    """One gate step of a static job, with the repository modules it runs and the input paths that key it."""

    name: str
    job: str
    step: str
    tools: tuple[str, ...]
    modules: tuple[str, ...]
    run: str
    paths: tuple[str, ...]
    base_paths: tuple[str, ...]


def discover(
    workflow_text: str, registry: Mapping[str, Any] | None = None, reusable: Mapping[str, str] | None = None
) -> tuple[StaticGate, ...]:
    """Return one StaticGate per gate step of the static jobs of a workflow, in job and step order."""
    return ()


def main(argv: Sequence[str] | None = None) -> int:
    """Print one JSON line per static gate of the repository."""
    return 0
