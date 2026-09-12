"""Project-local resources managed by AgentiHooks."""

from __future__ import annotations

import subprocess
from pathlib import Path


class ProjectResourceError(ValueError):
    pass


def find_project_root(cwd: str | Path | None = None) -> Path | None:
    start = Path(cwd) if cwd is not None else Path.cwd()
    start = start.expanduser()
    if not start.is_dir():
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def require_project_root(cwd: str | Path | None = None) -> Path:
    root = find_project_root(cwd)
    if root is None:
        location = Path(cwd).expanduser() if cwd is not None else Path.cwd()
        raise ProjectResourceError(f"not inside a Git project: {location}")
    return root


def project_resource_dir(cwd: str | Path | None = None, *, create: bool = False) -> Path:
    path = require_project_root(cwd) / ".agentihooks"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def project_resource_path(
    name: str | Path,
    cwd: str | Path | None = None,
    *,
    create_parent: bool = False,
) -> Path:
    relative = Path(name)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ProjectResourceError(f"invalid project resource path: {name}")
    path = project_resource_dir(cwd, create=create_parent) / relative
    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    return path
