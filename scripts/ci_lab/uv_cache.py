from pathlib import Path

__all__ = ["cache_for"]


def cache_for(source: Path | None, root: Path, timeout: float) -> Path:
    raise NotImplementedError
