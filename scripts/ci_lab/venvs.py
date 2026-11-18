"""Venv selection for the lab worker: the baked image venv when its lockfile matches, else one venv per lockfile."""

import fcntl
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from scripts.ci_lab.sandbox import PortAllocator

__all__ = ["BAKED", "MARKER", "Build", "venv_for"]

BAKED = Path("/opt/agentihooks-venv")
MARKER = "lockfile.sha256"


@dataclass(frozen=True)
class Build:
    """The sandbox a venv build runs in: its port ranges, the whitelisted environment and the time limit."""

    ports: PortAllocator
    whitelist: Mapping[str, str]
    timeout: float


def _marked(venv: Path, digest: str) -> bool:
    marker = venv / MARKER
    return marker.is_file() and marker.read_text().strip() == digest


def venv_for(lockfile_digest: str, tree: Path, root: Path, build: Build, baked: Path = BAKED) -> Path:
    """Return the baked venv when its marker holds the digest, else the digest's own venv under root, built once."""
    if _marked(baked, lockfile_digest):
        return baked
    venv = root / lockfile_digest
    root.mkdir(parents=True, exist_ok=True)
    with (root / f"{lockfile_digest}.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not _marked(venv, lockfile_digest):
            subprocess.run(
                ["uv", "sync", "--frozen", "--no-install-project", "--all-extras", "--project", str(tree)],
                check=True,
                env={**os.environ, **build.whitelist, "UV_PROJECT_ENVIRONMENT": str(venv)},
            )
            (venv / MARKER).write_text(f"{lockfile_digest}\n")
    return venv
