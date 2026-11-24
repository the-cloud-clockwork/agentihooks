"""Venv selection for the lab worker: the baked image venv when its lockfile matches, else one venv per lockfile,
built by uv from the head's tracked test requirements lock inside the lab sandbox with only its whitelisted
environment."""

import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from hooks.compat import fcntl
from scripts.ci_lab.sandbox import CannotRun, PortAllocator, Sandbox
from scripts.ci_mutation.stats_cache import EXCLUDES, LOCKFILE

__all__ = ["BAKED", "BUILDS", "EXCLUDES", "LOCKFILE", "MARKER", "Build", "venv_for"]

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
    """Build venv from tree in the sandbox with network, the whitelist and PATH: uv venv on the worker's Python, then
    uv pip install of the project's dev and all extras constrained by tree's tracked test lock, less its excluded
    packages, removing each step's run folder.

    Raises CannotRun when uv is not installed, or when a step ends with a nonzero code or its time limit, keeping
    its log.
    """
    uv = shutil.which("uv")
    if uv is None:
        raise CannotRun("uv is not installed, so a venv cannot be built")
    runs = root / BUILDS
    runs.mkdir(exist_ok=True)
    whitelist = {"PATH": os.defpath, **build.whitelist}
    constraints = ["--constraint", str(tree / LOCKFILE), "--excludes", str(tree / EXCLUDES)]
    project = ["-r", str(tree / "pyproject.toml"), "--extra", "dev", "--extra", "all"]
    steps = {
        "venv": [uv, "venv", "--clear", "--python", sys.executable, str(venv)],
        "pip install": [uv, "pip", "install", "--python", str(venv / "bin" / "python"), *constraints, *project],
    }
    for step, argv in steps.items():
        outcome = Sandbox(runs, tree, build.ports, whitelist).run(argv, build.timeout, net=True)
        if outcome.code != 0:
            raise CannotRun(f"uv {step} for {tree} ended with code {outcome.code}; its log is {outcome.log}")
        shutil.rmtree(outcome.log.parent)


def venv_for(lockfile_digest: str, tree: Path, root: Path, build: Build, baked: Path = BAKED) -> tuple[Path, bool]:
    """Return the baked venv when its marker holds the digest, else the digest's own venv under root, built once in
    the sandbox build names, and whether this call built it."""
    if _marked(baked, lockfile_digest):
        return baked, False
    venv = root / lockfile_digest
    root.mkdir(parents=True, exist_ok=True)
    with (root / f"{lockfile_digest}.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        built = not _marked(venv, lockfile_digest)
        if built:
            _sync(tree, venv, root, build)
            (venv / MARKER).write_text(f"{lockfile_digest}\n")
    return venv, built
