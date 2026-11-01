"""The fenced records port: per record reads and one all or nothing batch fenced by the controller lease."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

__all__ = [
    "BLIND",
    "FencedBatch",
    "LeaseFence",
    "Record",
    "RecordWrite",
    "Records",
    "RevisionConflict",
    "StaleLease",
]

BLIND = -1


@dataclass(frozen=True)
class Record:
    revision: int
    value: object


@dataclass(frozen=True)
class LeaseFence:
    owner: str
    epoch: int
    now_ms: int


@dataclass(frozen=True)
class RecordWrite:
    kind: Literal["put", "delete", "check"]
    section: str
    key: str
    expected: int
    value: object = None


@dataclass(frozen=True)
class FencedBatch:
    writes: tuple[RecordWrite, ...]
    lease: LeaseFence | None = None


class RevisionConflict(Exception):
    pass


class StaleLease(PermissionError):
    pass


@runtime_checkable
class Records(Protocol):
    def layout(self, slug: str) -> str: ...

    def sections(self, slug: str, names: Sequence[str]) -> dict[str, dict[str, Record]]: ...

    def record(self, slug: str, section: str, key: str) -> Record | None: ...

    def fenced_batch(self, slug: str, batch: FencedBatch) -> int: ...
