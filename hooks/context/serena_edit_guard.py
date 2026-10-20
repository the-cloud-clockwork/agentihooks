"""Serena Edit Guard — Python in a linked worktree is edited by symbol, never by built-in or shell rewrites.

Development Manifesto §7. Primary checkouts (dev-direct work), new files and deleting a whole file in the
session's own worktree stay open.
Formatters (ruff format, black) and patch / git apply stay open: they rewrite mechanically
and the gates require formatted code. AGENTIHOOKS_SERENA=false turns the guard off.
"""

import glob
import os
import re
import shlex
from itertools import takewhile
from pathlib import Path

from hooks.hook_manager import BlockAction
from scripts.swarm import naming

_FILE_TOOLS = frozenset({"Edit", "MultiEdit", "Write"})
_CD = re.compile(r"(?:^|&&|;|\()\s*cd\s+(\S+)\s*(?=&&|;)")
_PY = r"""['"]?([^\s'";|&<>()]+\.py)['"]?"""
_SHELL_WRITES = (
    re.compile(r"\bsed\b[^|;&\n]*\s(?:-i|--in-place)\S*\s[^|;&\n]*?" + _PY),
    re.compile(
        r"(?:^|[;&|\n(])\s*(?:(?:sudo|env|xargs|nohup)(?:\s+\S+)*?\s+)?(?:cp|mv|install)\s[^|;&\n]*\s"
        + _PY
        + r"\s*(?:$|[;&|\n])"
    ),
    re.compile(r"(?:^|\s)[12&]?>>?\s*" + _PY),
    re.compile(r"\bperl\b[^|;&\n]*\s-\w*i\S*\s[^|;&\n]*?" + _PY),
    re.compile(r"\bdd\b[^|;&\n]*\sof=" + _PY),
    re.compile(r"\btee\b(?:\s+-\S+)*\s+" + _PY),
    re.compile(r"""\bopen\(\s*""" + _PY + r"""\s*,\s*['"][wa]"""),
    re.compile(r"""Path\(\s*""" + _PY + r"""\s*\)\.write_(?:text|bytes)"""),
)
_SHELL_DELETE = re.compile(
    r"(?:^|[;&|\n(])\s*(?:(?:sudo|env|nohup|xargs|command|exec|time)(?:\s+\S+)*?\s+)?"
    r"(?:git\s+(?:-C\s+(\S+)\s+)?)?\\?(?:\S*/)?rm\s([^|;&\n]*)"
)
_FIND_DELETE = re.compile(r"\bfind\s([^|;&\n]*?)\s-(?:delete|exec(?:dir)?\s+(?:sudo\s+)?(?:\S*/)?(?:git\s+)?rm)\b")
_XARGS_RM = re.compile(r"([^|;&\n]*)\|\s*xargs\b[^|;&\n]*\brm\b")
_PATCH_ACTION = re.compile(r"^\*\*\* (Delete File|Add File|Update File|Move to): (.+)$", re.MULTILINE)


def _worktree_root(path: Path) -> Path | None:
    for d in (path, *path.parents):
        marker = d / ".git"
        if marker.is_dir():
            return None
        if marker.is_file():
            return d
    return None


def _checkout_root(path: Path) -> Path | None:
    for d in (path, *path.parents):
        if (d / ".git").exists():
            return d
    return None


def _guarded(path: Path) -> Path | None:
    if path.suffix != ".py" or not path.is_file():
        return None
    return _worktree_root(path.resolve())


def _block(path: Path, root: Path) -> None:
    raise BlockAction(
        f"BLOCKED: {path} is Python in the worktree {root}, which is edited through Serena "
        "(Development Manifesto §7).\n"
        f"Call mcp__serena__activate_project with {root}, then edit with replace_symbol_body, "
        "insert_after_symbol / insert_before_symbol, rename_symbol or replace_content. "
        "A new .py file may be created with Write; a whole file in this worktree is deleted with git rm or rm."
    )


def _block_foreign_delete(path: Path, root: Path) -> None:
    raise BlockAction(
        f"BLOCKED: {path} is Python in another checkout, {root}. "
        "A session deletes Python files only inside its own checkout."
    )


def _check_files(tool_input: dict, cwd: Path) -> None:
    session = _checkout_root(cwd.resolve())
    deleted = _patch_deletions(tool_input, cwd)
    for raw in filter(None, tool_input.get("file_paths") or [tool_input.get("file_path")]):
        path = Path(raw)
        full = path if path.is_absolute() else cwd / path
        if full.resolve() in deleted:
            _check_delete(path, full, session)
            continue
        root = _guarded(full)
        if root:
            _block(path, root)


def _patch_deletions(tool_input: dict, cwd: Path) -> set[Path]:
    if not tool_input.get("file_paths"):
        return set()
    actions: dict[Path, set[str]] = {}
    for action, target in _PATCH_ACTION.findall(str(tool_input.get("content"))):
        actions.setdefault((cwd / Path(target.strip())).resolve(), set()).add(action)
    return {path for path, kinds in actions.items() if kinds == {"Delete File"}}


def _own_worktree(root: Path) -> bool:
    base = Path(os.environ.get("WORKTREE_ROOT", Path.home() / "dev" / "worktrees")).resolve()
    if root.parent.parent != base:
        return False
    try:
        return naming.is_worktree(root.name, os.environ)
    except naming.NamingError:
        return False


def _foreign_root(target: Path, session: Path | None) -> Path | None:
    root = _checkout_root(target)
    if not root or root == session or _own_worktree(root):
        return None
    if target.is_dir():
        return root if any(target.rglob("*.py")) else None
    return root if target.suffix == ".py" and target.is_file() else None


def _check_delete(path: Path, full: Path, session: Path | None) -> None:
    root = _foreign_root(full.resolve(), session)
    if root:
        _block_foreign_delete(path, root)


def _tokens(text: str) -> list[str]:
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _shell_delete_targets(command: str, base: Path) -> list[tuple[str, Path]]:
    targets = []
    for match in _SHELL_DELETE.finditer(command):
        where = base / Path(_tokens(match.group(1))[0]).expanduser() if match.group(1) else base
        targets += [(token, where) for token in _tokens(match.group(2))]
    for match in _FIND_DELETE.finditer(command):
        starts = takewhile(lambda token: not token.startswith(("-", "(", "!")), _tokens(match.group(1)))
        targets += [(token, base) for token in [*starts] or ["."]]
    for match in _XARGS_RM.finditer(command):
        targets += [(token, base) for token in _tokens(match.group(1))]
    return targets


def _check_shell_deletes(command: str, base: Path, session: Path | None) -> None:
    for token, where in _shell_delete_targets(command, base):
        path = Path(token).expanduser()
        full = path if path.is_absolute() else where / path
        for name in glob.glob(str(full)) or [str(full)]:
            _check_delete(path, Path(name), session)


def check(payload: dict) -> None:
    from hooks.config import SERENA_EDIT_GUARD_ENABLED

    if not SERENA_EDIT_GUARD_ENABLED:
        return
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    cwd = Path(payload.get("cwd") or ".")

    if tool_name in _FILE_TOOLS:
        _check_files(tool_input, cwd)
        return

    if tool_name != "Bash":
        return
    command = tool_input.get("command", "")
    base = cwd
    for target in _CD.findall(command):
        base = base / Path(target.strip("'\"")).expanduser()
    for pattern in _SHELL_WRITES:
        for match in pattern.finditer(command):
            path = Path(match.group(1)).expanduser()
            root = _guarded(path if path.is_absolute() else base / path)
            if root:
                _block(path, root)
    _check_shell_deletes(command, base, _checkout_root(cwd.resolve()))
