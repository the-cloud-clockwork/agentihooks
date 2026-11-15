"""Lab check runs posted through the GitHub App with a token that carries only checks write.

`Checks.post` creates the check run `lab/<gate>`, or `lab-shadow/<gate>` for a shadow grade, of one head and
grade run on its first transition and advances it in place after, forward only through queued, in_progress and
completed. `Checks.batch` folds one scheduler pass to the last transition of each check run. A rate limited
answer backs off with the limits of the CI budget's GitHub API wrapper.
"""

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from scripts.ci_lab.units import Unit

__all__ = ["Checks", "Output", "PostRefused", "Token", "Transition", "name", "render"]


class PostRefused(RuntimeError):
    """GitHub refused a check run request with no rate limit wait left to retry it."""


class Token(Protocol):
    """A source of GitHub tokens, such as the App auth."""

    def get_token(self) -> str:
        """Return a token valid for the next request."""
        ...


@dataclass(frozen=True)
class Output:
    """What a check run shows: title, summary, the grade's units, run id, grader revision and details link."""

    title: str
    summary: str
    units: tuple[Unit, ...]
    run: str
    grader: str
    details: str


@dataclass(frozen=True)
class Transition:
    """One check run move a scheduler pass asks for."""

    head: str
    gate: str
    status: str
    conclusion: str | None
    output: Output
    shadow: bool


def name(gate: str, shadow: bool) -> str:
    """Return the check run name of a gate: lab/<gate>, or lab-shadow/<gate> for a shadow grade."""
    raise NotImplementedError


def render(output: Output) -> dict[str, str]:
    """Return the check run output: the summary with the run id, grader revision and details link, and every unit key."""
    raise NotImplementedError


class Checks:
    """The lab's check runs on one repository, posted with a token that carries only checks write."""

    def __init__(
        self,
        repo: str,
        auth: Token | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        raise NotImplementedError

    def post(self, head: str, gate: str, status: str, conclusion: str | None, output: Output, shadow: bool) -> int:
        """Create the check run on its first transition, else advance it in place; return its id."""
        raise NotImplementedError

    def batch(self, transitions: Iterable[Transition]) -> list[int]:
        """Post one pass of transitions, each check run once at its last move, refusing a bad move before any request."""
        raise NotImplementedError
