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
    """Return the interpreter of the environment for root's lock, building it and removing older ones when missing."""
    target = home / key(root)
    python = target / "bin" / "python"
    if (target / READY).is_file():
        return python
    home.mkdir(parents=True, exist_ok=True)
    for stale in home.iterdir():
        if stale.is_dir():
            shutil.rmtree(stale)
    execute(["uv", "venv", "-q", "--python", PYTHON, str(target)], check=True)
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
    (target / READY).write_text(key(root) + "\n")
    return python


def main(argv: list[str] | None = None) -> int:
    """Run a type gate copy with the CI matched interpreter, forwarding every argument it does not own."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--grader", type=Path, default=Path("scripts/type_gate.py"))
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args, forwarded = parser.parse_known_args(argv)
    try:
        python = environment(args.root)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"::error::The CI type environment could not be built: {exc}")
        return 1
    return subprocess.run([str(python), "-I", str(args.grader.resolve()), *forwarded], cwd=args.root).returncode


if __name__ == "__main__":
    raise SystemExit(main())
