from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

PRIMARY = "primary"
WORKTREE = "worktree"


class BindingError(ValueError):
    pass


@dataclass(eq=False)
class Binding:
    root: Path | None = None
    kind: str = ""


def _rev_parse(path: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path), "rev-parse", *args],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if proc.returncode != 0:
        raise BindingError(f"{path} is not inside a git checkout")
    return proc.stdout.strip()


def resolve(path: str) -> tuple[Path, str]:
    if not path or not Path(path).is_absolute():
        raise BindingError("activate_project needs the absolute path of your worktree")
    target = Path(path)
    if not target.is_dir():
        raise BindingError(f"{path} does not exist")
    root = Path(_rev_parse(target, "--show-toplevel")).resolve()
    git_dir = Path(_rev_parse(root, "--absolute-git-dir")).resolve()
    common = Path(_rev_parse(root, "--path-format=absolute", "--git-common-dir")).resolve()
    return root, PRIMARY if git_dir == common else WORKTREE


def _serena_config_tracked(root: Path) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--error-unmatch", ".serena/project.yml"],
        capture_output=True,
        timeout=10,
        check=False,
    )
    return proc.returncode == 0


def ensure_excluded(root: Path) -> None:
    """Serena writes .serena/ into every project it opens; an untracked one blocks `wt.sh done`."""
    if _serena_config_tracked(root):
        return
    exclude = Path(_rev_parse(root, "--path-format=absolute", "--git-common-dir")) / "info" / "exclude"
    lines = exclude.read_text().splitlines() if exclude.exists() else []
    if ".serena/" in lines:
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a") as fh:
        fh.write(("\n" if lines and lines[-1] else "") + ".serena/\n")
