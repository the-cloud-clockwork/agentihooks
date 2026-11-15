"""Venv selection for the lab worker: the baked image venv when its lockfile matches, else one venv per lockfile."""

import os
import subprocess
from pathlib import Path

__all__ = ["BAKED", "MARKER", "venv_for"]

BAKED = Path("/opt/agentihooks-venv")
MARKER = "lockfile.sha256"


def _marked(venv: Path, digest: str) -> bool:
    marker = venv / MARKER
    return marker.is_file() and marker.read_text().strip() == digest


def venv_for(lockfile_digest: str, tree: Path, root: Path, baked: Path = BAKED) -> Path:
    """Return the baked venv when its marker holds the digest, else the digest's own venv under root, built once."""
    if _marked(baked, lockfile_digest):
        return baked
    venv = root / lockfile_digest
    if not _marked(venv, lockfile_digest):
        subprocess.run(
            ["uv", "sync", "--frozen", "--no-install-project", "--all-extras", "--project", str(tree)],
            check=True,
            env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(venv)},
        )
        (venv / MARKER).write_text(f"{lockfile_digest}\n")
    return venv
