from dataclasses import dataclass
from typing import NewType, Protocol

from scripts.tick.deadline import Deadline

__all__ = ["Context", "Domain", "Lane", "Result", "Step", "View", "Write"]

Domain = NewType("Domain", str)
Lane = NewType("Lane", str)


class View(Protocol):
    def read(self, domain: Domain, key: str) -> object | None: ...


@dataclass(frozen=True)
class Write:
    domain: Domain
    key: str
    value: object


@dataclass(frozen=True)
class Result:
    writes: tuple[Write, ...] = ()


@dataclass(frozen=True)
class Context:
    view: View
    deadline: Deadline


class Step(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def group(self) -> str: ...

    @property
    def lane(self) -> Lane: ...

    @property
    def order(self) -> int: ...

    @property
    def budget_s(self) -> float: ...

    @property
    def interval_s(self) -> float: ...

    @property
    def needs(self) -> frozenset[str]: ...

    @property
    def writes(self) -> frozenset[Domain]: ...

    def run(self, ctx: Context) -> Result: ...
