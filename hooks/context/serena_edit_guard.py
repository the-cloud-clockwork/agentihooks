"""Serena Edit Guard — Python in a linked worktree is edited by symbol, never by built-in or shell rewrites.

Development Manifesto §7. Primary checkouts (dev-direct work), new files and deleting a whole file in the
session's own worktree stay open.
Formatters (ruff format, black) and patch / git apply stay open: they rewrite mechanically
and the gates require formatted code. AGENTIHOOKS_SERENA=false turns the guard off.
"""

import re
import shlex
from pathlib import Path

from hooks.hook_manager import BlockAction

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
_SHELL_DELETE = re.compile(r"(?:^|[;&|\n(])\s*(?:git\s+)?rm\s([^|;&\n]*)")
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
    for raw in tool_input.get("file_paths") or [tool_input.get("file_path") or ""]:
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
    for action, target in _PATCH_ACTION.findall(tool_input.get("content") or ""):
        actions.setdefault((cwd / Path(target.strip())).resolve(), set()).add(action)
    return {path for path, kinds in actions.items() if kinds == {"Delete File"}}


def _check_delete(path: Path, full: Path, session: Path | None) -> None:
    if full.suffix != ".py" or not full.is_file():
        return
    root = _checkout_root(full.resolve())
    if root and root != session:
        _block_foreign_delete(path, root)


def _check_shell_deletes(command: str, base: Path, session: Path | None) -> None:
    for match in _SHELL_DELETE.finditer(command):
        try:
            tokens = shlex.split(match.group(1))
        except ValueError:
            tokens = match.group(1).split()
        for token in tokens:
            path = Path(token).expanduser()
            _check_delete(path, path if path.is_absolute() else base / path, session)


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
