"""Venv Guard — blocks two writes to a venv the command's project does not own.

`uv run|sync --active` makes uv sync $VIRTUAL_ENV to the current project's
lockfile. When that venv is shared (a workspace venv several editable repos
install into), the sync up- and downgrades packages the other repos depend on.

An editable install records its source folder in the venv. Into the venv the
hooks run from, a source outside a primary checkout, such as a worktree that
wt.sh done later removes, leaves every hook and gate condition importing from
a deleted folder, so they fail open.
"""

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from hooks.context.shell_commands import located
from hooks.hook_manager import BlockAction

_ACTIVE_SYNC_RE = re.compile(r"\buv\s+(?:run|sync)\b[^|&;\n]*--active\b")
_PIP = re.compile(r"pip[\d.]*")
_PYTHON = re.compile(r"python[\d.]*")
_EDITABLE_OPTION = re.compile(r"(?:-e|--editable)(?:=?(.+))?")
_PYTHON_OPTION = re.compile(r"(?:--python|-p)(?:=(.+))?")
_ASSIGNS_VENV = re.compile(r"\b(?:VIRTUAL_ENV|UV_PYTHON)=")
_EXTRAS = re.compile(r"\[[^\]]*\]$")


def _project_root(cwd: Path) -> Path | None:
    for d in (cwd, *cwd.parents):
        if (d / "pyproject.toml").is_file():
            return d
    return None


def _expanded(word: str) -> str:
    return os.path.expanduser(os.path.expandvars(word))


def _cwd_after_cds(cwd: Path, folders: list[str]) -> Path:
    """The folder a command runs in once the cd words before it have run."""
    for folder in folders:
        target = cwd / _expanded(folder)
        if target.is_dir():
            cwd = target
    return cwd


def _installs(tokens: list[str]) -> bool:
    """Whether a command runs pip install, as uv pip install, a pip program or python -m pip."""
    name, words = Path(tokens[0]).name, tokens[1:]
    if name == "uv" or _PYTHON.fullmatch(name):
        words = words[words.index("pip") + 1 :] if "pip" in words else []
    elif not _PIP.fullmatch(name):
        return False
    return "install" in words


def _sources(words: list[str]) -> list[str]:
    """The folder each -e or --editable option names, written attached, after an equals sign or as the next word."""
    return [
        _EXTRAS.sub("", value)
        for word, after in zip(words, [*words[1:], None])
        if (match := _EDITABLE_OPTION.fullmatch(word)) and (value := match[1] or after)
    ]


def _named_target(tokens: list[str], cwd: Path) -> Path | None:
    """The venv path an install names itself: its --python or -p value, else its program's path."""
    for word, after in zip(tokens, [*tokens[1:], None]):
        if (match := _PYTHON_OPTION.fullmatch(word)) and (value := match[1] or after):
            return cwd / _expanded(value)
    if "/" in tokens[0]:
        return cwd / _expanded(tokens[0])
    return None


def _environment_target(tokens: list[str], cwd: Path) -> Path | None:
    """The venv an install that names none writes into: $VIRTUAL_ENV, else for uv the nearest .venv."""
    if active := os.environ.get("VIRTUAL_ENV"):
        return Path(active)
    if Path(tokens[0]).name == "uv":
        return next((folder / ".venv" for folder in (cwd, *cwd.parents) if (folder / ".venv").is_dir()), None)
    return None


def _into_hooks_venv(tokens: list[str], cwd: Path, assigns_venv: bool) -> bool:
    """Whether an install writes into the venv the hooks run from, or into one the guard cannot read: a path an
    unexpanded variable hides, or a venv the command picks by assigning VIRTUAL_ENV or UV_PYTHON."""
    target = _named_target(tokens, cwd)
    if target is None and assigns_venv:
        return True
    target = target or _environment_target(tokens, cwd)
    if target is None or "$" in str(target):
        return target is not None
    venv = target if target.is_dir() else target.parent.parent
    return venv.resolve() == Path(sys.prefix).resolve()


def _in_primary_checkout(folder: Path) -> bool:
    """Whether a folder lies in a repository's main checkout, whose git dir is the common dir its worktrees share."""
    found = subprocess.run(
        ["git", "-C", str(folder), "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"],
        capture_output=True,
        text=True,
    )
    dirs = found.stdout.split("\n")
    return found.returncode == 0 and dirs[0] == dirs[1]


def _editable_refusal(command: str, cwd: Path) -> str:
    """The refusal for an editable install into the hooks' venv whose source is not inside a primary checkout."""
    assigns_venv = bool(_ASSIGNS_VENV.search(command))
    for tokens, folders in located(command):
        if not _installs(tokens):
            continue
        here = _cwd_after_cds(cwd, folders)
        if _into_hooks_venv(tokens, here, assigns_venv):
            for source in _sources(tokens):
                folder = here / _expanded(source)
                if not _in_primary_checkout(folder):
                    return (
                        f"BLOCKED: this editable install can point the venv the hooks run from ({sys.prefix}) at "
                        f"{folder}, which is not inside a primary checkout. Once that folder goes, as wt.sh done "
                        "removes a worktree, every hook and gate condition that imports from it fails open.\n"
                        f"Install from the primary checkout (uv pip install --python {sys.prefix}/bin/python -e "
                        "<primary checkout>), or name the worktree's own venv with --python."
                    )
    return ""


def check_venv_guard(payload: dict[str, Any]) -> None:
    from hooks.context._strip import strip_non_command_content
    from hooks.context.branch_guard import _resolve_cwd

    command = payload.get("tool_input", {}).get("command", "")
    if refusal := _editable_refusal(command, Path(payload.get("cwd") or os.getcwd())):
        raise BlockAction(refusal)
    venv = os.environ.get("VIRTUAL_ENV", "")
    if not command or not venv or not _ACTIVE_SYNC_RE.search(strip_non_command_content(command)):
        return

    root = _project_root(Path(_resolve_cwd(command, payload.get("cwd", ""))).resolve())
    if root and Path(venv).resolve() == (root / ".venv").resolve():
        return

    raise BlockAction(
        f"BLOCKED: `uv --active` would sync {venv} to {root or 'this project'}'s lockfile, "
        "and that venv is not this project's own .venv. Other projects installed in it "
        "would lose their package versions.\n"
        f"Run tools from the venv directly ({venv}/bin/python -m pytest), or add one "
        f"package with `uv pip install --python {venv}/bin/python <pkg>`."
    )
