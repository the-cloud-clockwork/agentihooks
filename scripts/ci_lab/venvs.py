"""Venv selection for the lab worker: the baked image venv when its lockfile matches, else one venv per lockfile,
built by uv sync inside the lab sandbox with only its whitelist."""

import fcntl
import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from scripts.ci_lab.sandbox import CannotRun, PortAllocator, Sandbox

__all__ = ["BAKED", "BUILDS", "MARKER", "Build", "venv_for"]

BAKED = Path("/opt/agentihooks-venv")
MARKER = "lockfile.sha256"
BUILDS = "builds"


@dataclass(frozen=True)
class Build:
    """The sandbox a venv build runs in: its port ranges, the whitelisted environment and the time limit."""

    ports: PortAllocator
    whitelist: Mapping[str, str]
    timeout: float


def _marked(venv: Path, digest: str) -> bool:
    marker = venv / MARKER
    return marker.is_file() and marker.read_text().strip() == digest


def _sync(tree: Path, venv: Path, root: Path, build: Build) -> None:
    """Run the worker's uv sync of tree into venv from tree, in the sandbox with network, the whitelist, PATH and the
    venv, then remove its run folder.

    Raises CannotRun when uv is not installed, or when the build ends with a nonzero code or its time limit, keeping
    its log.
    """
    uv = shutil.which("uv")
    if uv is None:
        raise CannotRun("uv is not installed, so a venv cannot be built")
    runs = root / BUILDS
    runs.mkdir(exist_ok=True)
    whitelist = {"PATH": os.defpath, **build.whitelist, "UV_PROJECT_ENVIRONMENT": str(venv)}
    argv = [uv, "sync", "--frozen", "--no-install-project", "--all-extras", "--python", sys.executable]
    outcome = Sandbox(runs, tree, build.ports, whitelist).run([*argv, "--project", str(tree)], build.timeout, net=True)
    if outcome.code != 0:
        raise CannotRun(f"uv sync for {tree} ended with code {outcome.code}; its log is {outcome.log}")
    shutil.rmtree(outcome.log.parent)


def venv_for(lockfile_digest: str, tree: Path, root: Path, build: Build, baked: Path = BAKED) -> Path:
    """Return the baked venv when its marker holds the digest, else the digest's own venv under root, built once in
    the sandbox build names."""
    if _marked(baked, lockfile_digest):
        return baked
    venv = root / lockfile_digest
    root.mkdir(parents=True, exist_ok=True)
    with (root / f"{lockfile_digest}.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not _marked(venv, lockfile_digest):
            _sync(tree, venv, root, build)
            (venv / MARKER).write_text(f"{lockfile_digest}\n")
    return venv
