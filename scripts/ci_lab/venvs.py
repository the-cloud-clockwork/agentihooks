"""Venv selection for the lab worker: the baked image venv when its lockfile matches, else one venv per lockfile."""

from pathlib import Path

__all__ = ["BAKED", "MARKER", "venv_for"]

BAKED = Path("/opt/agentihooks-venv")
MARKER = "lockfile.sha256"


def venv_for(lockfile_digest: str, tree: Path, root: Path, baked: Path = BAKED) -> Path:
    """Return the baked venv when its marker holds the digest, else the digest's own venv under root, built once."""
    raise NotImplementedError(lockfile_digest, tree, root, baked)
