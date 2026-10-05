"""Serena Edit Guard — Python in a linked worktree is edited by symbol, never by built-in or shell rewrites.

Development Manifesto §7. Primary checkouts (dev-direct work) and new files stay open.
Formatters (ruff format, black) and patch / git apply stay open: they rewrite mechanically
and the gates require formatted code.
"""

import re
from pathlib import Path

from hooks.hook_manager import BlockAction

_FILE_TOOLS = frozenset({"Edit", "MultiEdit", "Write"})
_CD = re.compile(r"(?:^|&&|;|\()\s*cd\s+(\S+)\s*(?=&&|;)")
_PY = r"""['"]?([^\s'";|&<>()]+\.py)['"]?"""
_SHELL_WRITES = (
    re.compile(r"\bsed\b[^|;&\n]*\s(?:-i|--in-place)\S*\s[^|;&\n]*?" + _PY),
    re.compile(r"(?:^|[;&|\n])\s*(?:sudo\s+)?(?:cp|mv|install)\s[^|;&\n]*\s" + _PY + r"\s*(?:$|[;&|\n])"),
    re.compile(r"(?:^|\s)[12&]?>>?\s*" + _PY),
    re.compile(r"\bperl\b[^|;&\n]*\s-\w*i\S*\s[^|;&\n]*?" + _PY),
    re.compile(r"\bdd\b[^|;&\n]*\sof=" + _PY),
    re.compile(r"\btee\b(?:\s+-\S+)*\s+" + _PY),
    re.compile(r"""\bopen\(\s*""" + _PY + r"""\s*,\s*['"][wa]"""),
    re.compile(r"""Path\(\s*""" + _PY + r"""\s*\)\.write_(?:text|bytes)"""),
)


def _worktree_root(path: Path) -> Path | None:
    for d in (path, *path.parents):
        marker = d / ".git"
        if marker.is_dir():
            return None
        if marker.is_file():
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
        "A new .py file may be created with Write."
    )


def check(payload: dict) -> None:
    from hooks.config import SERENA_EDIT_GUARD_ENABLED

    if not SERENA_EDIT_GUARD_ENABLED:
        return
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    cwd = Path(payload.get("cwd") or ".")

    if tool_name in _FILE_TOOLS:
        path = Path(tool_input.get("file_path") or "")
        root = _guarded(path if path.is_absolute() else cwd / path)
        if root:
            _block(path, root)
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
