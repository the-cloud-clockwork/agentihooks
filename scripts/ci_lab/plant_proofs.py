from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Pair:
    name: str
    base: str
    green: str
    red: str
    gate: str
    reason: str


def prepare(root: Path, name: str, base: str) -> Pair:
    raise NotImplementedError
