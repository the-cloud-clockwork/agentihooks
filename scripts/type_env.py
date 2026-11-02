"""Run the strict type gate in a virtual environment built from the CI test lock.

basedpyright resolves third party types from the interpreter it is given, so a workstation venv that holds other
package versions counts other errors than the CI type gate job. This builds the same environment the CI job installs,
once per lock, and runs a type gate copy with it.
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

PYTHON = "3.12"
LOCK = ".github/test-requirements.lock"
CONSTRAINTS = ".github/test-constraints.txt"
EXCLUDES = ".github/test-excludes.txt"
INPUTS = ("pyproject.toml", LOCK, CONSTRAINTS, EXCLUDES)
EXTRAS = ("dev", "all")
HOME = Path.home() / ".cache" / "agentihooks" / "type-env"
READY = "ready"
STALE_SECONDS = 7 * 24 * 3600


def key(root: Path) -> str:
    """Return a digest of the Python version and every file that decides the CI test environment's packages."""
    digest = hashlib.sha256(PYTHON.encode())
    for name in INPUTS:
        path = root / name
        digest.update(f"\0{name}\0".encode())
        digest.update(path.read_bytes() if path.is_file() else b"")
    return digest.hexdigest()[:16]


def environment(
    root: Path, home: Path = HOME, execute: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run
) -> Path:
    """Return the interpreter of the environment for root's lock, building it when missing.

    Each use marks its environment, and a build removes only environments unused for STALE_SECONDS, so a concurrent
    run on another lock keeps its own.
    """
    target = home / key(root)
    python = target / "bin" / "python"
    ready = target / READY
    if ready.is_file() and python.is_file():
        ready.touch()
        return python
    home.mkdir(parents=True, exist_ok=True)
    execute(["uv", "venv", "-q", "--clear", "--python", PYTHON, str(target)], check=True)
    execute(
        [
            "uv",
            "pip",
            "install",
            "-q",
            "--python",
            str(python),
            "--constraint",
            str(root / LOCK),
            "--excludes",
            str(root / EXCLUDES),
            "-e",
            f"{root}[{','.join(EXTRAS)}]",
        ],
        env={**os.environ, "UV_CONSTRAINT": str(root / CONSTRAINTS)},
        check=True,
    )
    ready.write_text(key(root) + "\n")
    cutoff = time.time() - STALE_SECONDS
    for stale in home.iterdir():
        marker = stale / READY
        if stale != target and not stale.is_symlink() and stale.is_dir():
            if (marker.stat().st_mtime if marker.is_file() else stale.stat().st_mtime) < cutoff:
                shutil.rmtree(stale)
    return python


def main(argv: list[str] | None = None) -> int:
    """Run a type gate copy with the CI matched interpreter, forwarding every argument it does not own."""
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--grader", type=Path, default=Path("scripts/type_gate.py"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args, forwarded = parser.parse_known_args(argv)
    try:
        python = environment(args.root)
        return subprocess.run([str(python), "-I", str(args.grader.resolve()), *forwarded], cwd=args.root).returncode
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"::error::The CI type environment could not run the type gate: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
