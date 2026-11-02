"""Provider neutral records the source control port reads and writes."""

from dataclasses import dataclass

__all__ = [
    "Capabilities",
    "ChangeRef",
    "ChangeState",
    "CheckState",
    "Comment",
    "Merged",
    "OpenChange",
    "QueueEntry",
    "QueueHealth",
    "RateLimit",
    "RepositoryRun",
    "RollupCheck",
    "Run",
    "Tag",
]


@dataclass(frozen=True)
class ChangeRef:
    repository: str
    number: int


@dataclass(frozen=True)
class ChangeState:
    reference: ChangeRef
    state: str
    head: str
    base: str
    merged: bool
    url: str
    head_branch: str = ""
    body: str = ""
    draft: bool = False
    head_repository: str = ""


@dataclass(frozen=True)
class CheckState:
    name: str
    status: str
    conclusion: str | None
    url: str


@dataclass(frozen=True)
class RollupCheck:
    name: str
    result: str | None
    at: str | None


@dataclass(frozen=True)
class OpenChange:
    url: str
    checks: tuple[RollupCheck, ...]


@dataclass(frozen=True)
class Run:
    id: int
    name: str
    head: str
    status: str
    conclusion: str | None
    url: str


@dataclass(frozen=True)
class RepositoryRun:
    id: int
    branch: str | None
    event: str
    status: str


@dataclass(frozen=True)
class QueueEntry:
    reference: ChangeRef
    position: int


@dataclass(frozen=True)
class QueueHealth:
    depth: int
    states: tuple[str, ...]


@dataclass(frozen=True)
class Merged:
    reference: ChangeRef
    sha: str


@dataclass(frozen=True)
class Tag:
    name: str
    sha: str


@dataclass(frozen=True)
class Comment:
    reference: ChangeRef
    url: str


@dataclass(frozen=True)
class Capabilities:
    merge_queue: bool


@dataclass(frozen=True)
class RateLimit:
    used: int
    remaining: int
    reset: int
