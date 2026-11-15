"""GitHub Actions expressions in the workflow steps the lab runs: a step's if condition and the ${{ }} in its env."""

from collections.abc import Mapping

__all__ = ["Value", "evaluate", "holds", "interpolate"]

Value = str | bool


def evaluate(expression: str, context: Mapping[str, str]) -> Value:
    """Return the value of one expression, reading context names from context and an absent name as empty."""
    raise NotImplementedError


def interpolate(text: str, context: Mapping[str, str]) -> str:
    """Return text with each ${{ }} expression replaced by the text of its value."""
    raise NotImplementedError


def holds(condition: str, context: Mapping[str, str]) -> bool:
    """True when a step's if condition holds in a lab run; an empty condition holds."""
    raise NotImplementedError
