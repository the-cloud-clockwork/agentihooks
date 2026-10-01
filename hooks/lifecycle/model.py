from dataclasses import dataclass

ACTIONABLE = ("remove", "snapshot", "archive")


@dataclass(frozen=True)
class Root:
    id: str
    path: str
    kind: str
    idle_days: float = 7.0
    budget_gb: float = 0.0
    include: tuple[str, ...] = ()


@dataclass(frozen=True)
class Holder:
    session_id: str
    pid: int
    start_time: int
    boot_id: str


@dataclass(frozen=True)
class Lease:
    kind: str
    created_at: float
    holders: tuple[Holder, ...] = ()


@dataclass(frozen=True)
class Finding:
    path: str
    root: str
    category: str
    action: str
    reason: str
    last_touch: float = 0.0
    size: int = 0
    due: bool = False
    outcome: str = ""
