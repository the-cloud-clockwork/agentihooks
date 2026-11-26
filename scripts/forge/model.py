"""The change request a forge reads and the protocol every forge implements."""

from dataclasses import asdict, dataclass
from typing import Any, Protocol


class ForgeError(RuntimeError):
    """A forge call failed."""


@dataclass(frozen=True)
class Check:
    name: str
    state: str


@dataclass(frozen=True)
class ChangeRequest:
    number: int
    url: str
    title: str
    body: str
    head: str
    diff: str
    files: tuple[str, ...]
    checks: tuple[Check, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class Forge(Protocol):
    def group_pulls(self, base: str, head: str) -> tuple[int, ...]: ...

    def read(self, number: int) -> ChangeRequest: ...

    def post(self, number: int, body: str) -> str: ...

    def release_pull(self, tag: str) -> int: ...
