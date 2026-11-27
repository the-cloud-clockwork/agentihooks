from collections.abc import Callable
from pathlib import Path


def missing(source: Path, home: Path, _configuration: Callable[[Path, Path], None]) -> None:
    # ruleid: native-default-codex-trust
    _configuration(source, home)


def too_early(
    source: Path,
    home: Path,
    agent: str,
    _configuration: Callable[[Path, Path], None],
    _codex_trust: Callable[[Path, Path], None],
) -> None:
    if agent == "codex":
        _codex_trust(source, home)
    # ruleid: native-default-codex-trust
    _configuration(source, home)


def rebased(
    source: Path,
    home: Path,
    agent: str,
    _configuration: Callable[[Path, Path], None],
    _codex_trust: Callable[[Path, Path], None],
) -> None:
    # ok: native-default-codex-trust
    _configuration(source, home)
    if agent == "codex":
        _codex_trust(source, home)
