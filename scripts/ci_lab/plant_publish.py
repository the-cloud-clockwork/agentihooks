from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from scripts.ci_lab.plant_proofs import Pair

Request = Callable[[str, str, Mapping[str, Any] | None], Any]


def publish(root: Path, pair: Pair, head: str, repository: str, request: Request | None = None) -> None:
    raise NotImplementedError
