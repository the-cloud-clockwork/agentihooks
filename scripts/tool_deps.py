"""Declared dependencies of the checkout the agentihooks command runs from, kept installed in its tool environment.

The tool install is editable on the main checkout, so a dev pull brings new code but never the dependencies
that code newly declares; a pull through `pull` installs them.
"""

from __future__ import annotations

import importlib.metadata
import re
import subprocess
import tomllib
from collections.abc import Callable
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
TIMEOUT_SECONDS = 600
_NAME = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def declared(text: str) -> list[str]:
    return list(tomllib.loads(text).get("project", {}).get("dependencies", []))


def missing(specs: list[str], found: Callable = importlib.metadata.distribution) -> list[str]:
    absent = []
    for spec in specs:
        try:
            found(_NAME.match(spec).group(1))
        except importlib.metadata.PackageNotFoundError:
            absent.append(spec)
    return absent


def missing_here() -> list[str]:
    path = SOURCE / "pyproject.toml"
    return missing(declared(path.read_text())) if path.is_file() else []


def call(argv: list[str]) -> str:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{' '.join(argv)} timed out after {exc.timeout} seconds") from exc
    if done.returncode:
        raise RuntimeError(f"{' '.join(argv)} failed: {(done.stderr or done.stdout).strip()}")
    return done.stdout.strip()


def install(specs: list[str], run: Callable, python: str) -> list[str]:
    if specs:
        run(["uv", "pip", "install", "--python", python, *specs])
    return specs


def _head(repo: str, run: Callable) -> list[str]:
    return declared(run(["git", "-C", repo, "show", "HEAD:pyproject.toml"]))


def pull(repo: str, run: Callable, python: str, source: Path = SOURCE) -> list[str]:
    tracked = Path(repo).resolve() == Path(source).resolve()
    before = _head(repo, run) if tracked else []
    run(["git", "-C", repo, "pull", "--ff-only", "origin", "dev"])
    if not tracked:
        return []
    after = _head(repo, run)
    changed = [spec for spec in after if spec not in before]
    return install(changed + [spec for spec in missing(after) if spec not in changed], run, python)
