from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BRANCH_PREFIX = "refs/heads/diffcheck/lab-plant-"


@dataclass(frozen=True)
class Ports:
    fetch: Callable[[str], Mapping[str, Any]]
    sleep: Callable[[float], None]
    clock: Callable[[], float]


def prove(
    root: Path,
    name: str,
    base: str,
    repository: str,
    ports: Ports,
    timeout_s: float = 900,
) -> dict[str, str]:
    raise NotImplementedError


def main(argv: Sequence[str], environ: Mapping[str, str], ports: Ports | None = None) -> int:
    raise NotImplementedError
