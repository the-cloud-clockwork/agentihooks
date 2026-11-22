"""The lab worker's writable uv cache: a private copy of the node's read only uv cache for offline venv builds."""

from pathlib import Path

__all__ = ["cache_for"]


def cache_for(source: Path | None, root: Path) -> Path:
    """Return the writable uv cache under root that venv builds read offline."""
    raise NotImplementedError
