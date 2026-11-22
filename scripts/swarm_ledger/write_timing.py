from dataclasses import dataclass
from pathlib import Path

TEXT = "Timing one agent ledger write"


@dataclass(frozen=True)
class Child:
    code: int
    wall: float
    cpu: float


def spawned(argv: list[str], timing: Path) -> Child:
    raise NotImplementedError


def main(argv: list[str] | None = None) -> None:
    raise NotImplementedError
