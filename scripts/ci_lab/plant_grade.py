from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.ci_lab.head_notice import Opener


@dataclass(frozen=True)
class Ports:
    fetch: Callable[[str], Mapping[str, Any]]
    opener: Opener
    sleep: Callable[[float], None]
    clock: Callable[[], float]


def prove(
    root: Path,
    name: str,
    base: str,
    repository: str,
    environ: Mapping[str, str],
    ports: Ports,
    timeout_s: float = 900,
) -> dict[str, str]:
    raise NotImplementedError
