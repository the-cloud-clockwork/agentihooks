"""Grade comments in Python, shell, JavaScript and CSS files against a merge base tree."""

import argparse
import ast
import functools
import importlib
import io
import json
import re
import subprocess
import tokenize
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

REGISTRY = "tests/COMMENT_FREE_PATHS.json"
_CODES = r"[A-Z]+\d+(?:\s*,\s*[A-Z]+\d+)*"
_NAMES = r"[\w.-]+(?:\s*,\s*[\w.-]+)*"
_DIRECTIVES = (
    rf"noqa(?::\s*{_CODES})?",
    rf"type:\s*ignore(?:\[{_NAMES}\])?",
    r"pragma:\s*no (?:cover|branch|mutate)",
    rf"pyright:\s*ignore(?:\[{_NAMES}\])?",
    r"pyright:\s*(?:strict|basic|standard|off)",
    r"pyright:\s*\w+\s*=\s*(?:true|false|error|warning|information|none)(?:\s*,\s*\w+\s*=\s*(?:true|false|error|warning|information|none))*",
    rf"ruff:\s*noqa(?::\s*{_CODES})?",
    r"fmt:\s*(?:off|on|skip)",
    r"isort:\s*(?:skip|skip_file|off|on|split|dont-add-imports)",
    rf"nosec(?:\s+{_CODES})?",
    r"NOSONAR",
    rf"nosemgrep(?::\s*{_NAMES})?",
)
_DIRECTIVE = re.compile("(?:" + "|".join(_DIRECTIVES) + ")")
_COOKIE = re.compile(r"(?:-\*-\s*)?coding[:=]\s*[-\w.]+(?:\s*-\*-)?")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
STATIC = "scripts/swarm_ledger/static/"
_GRAMMARS = {"shell": "tree_sitter_bash", "javascript": "tree_sitter_javascript", "css": "tree_sitter_css"}
_BASH = re.compile(rb"#!\s*(?:/usr/bin/env\s+(?:-\S+\s+)*)?(?:\S*/)?bash(?:\s|$)")
_TREE_DIRECTIVES = {
    "shell": re.compile(r"shellcheck(?:\s+[a-z-]+=[\w./,-]+)+"),
    "javascript": re.compile(
        r"(?:eslint-(?:disable(?:-next-line|-line)?|enable)(?:\s+[\w@/-]+(?:\s*,\s*[\w@/-]+)*)?)"
        r"|@ts-(?:ignore|expect-error|nocheck|check)"
    ),
    "css": re.compile(r"(?!)"),
}
_DOCUMENTED = {"function_declaration", "generator_function_declaration", "class_declaration", "method_definition"}
_FUNCTIONS = {"function_expression", "arrow_function", "generator_function", "class"}
HEADER_LINES = 20


def _tracked(root: Path) -> list[str]:
    result = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True)
    return sorted(set(result.stdout.decode().split("\0")) - {""})


def _directive(token: tokenize.TokenInfo, name: str) -> bool:
    text = token.string[1:].strip()
    if token.start == (1, 0) and text.startswith("!"):
        return True
    if token.start[0] <= 2 and not token.line[: token.start[1]].strip() and _COOKIE.fullmatch(text):
        return True
    if _annotation(name, text):
        return True
    return _DIRECTIVE.fullmatch(text) is not None


def _annotation(name: str, text: str) -> bool:
    fixture = any(part.startswith(".semgrep") for part in Path(name).parts[:-1])
    return fixture and re.fullmatch(rf"(?:ruleid|ok|todoruleid|todook):\s*{_NAMES}", text) is not None


def violations(source: str, name: str) -> list[tuple[int, int, str]]:
    tree = ast.parse(source, filename=name)
    allowed = set()
    found = []
    for owner in ast.walk(tree):
        if not isinstance(owner, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) or not owner.body:
            continue
        first = owner.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            allowed.add(id(first))
            if isinstance(owner, ast.Module) and first.end_lineno - first.lineno + 1 > 20:
                found.append((first.lineno, first.end_lineno, "module docstring exceeds 20 lines"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and id(node) not in allowed:
            if (
                isinstance(node.value, ast.JoinedStr)
                or isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                found.append((node.lineno, node.end_lineno, "forbidden string statement"))
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT and not _directive(token, name):
            found.append((token.start[0], token.end[0], "forbidden comment"))
    return sorted(found)


def language(root: Path, name: str) -> str | None:
    """Name the grammar a tracked file's comments are graded with, or None when the file is not graded."""
    if name.endswith(".py"):
        return "python"
    if name.startswith(STATIC) and name.endswith((".js", ".css")):
        return "javascript" if name.endswith(".js") else "css"
    if name.endswith(".sh"):
        return "shell"
    path = root / name
    if path.is_symlink() or not path.is_file():
        return None
    with path.open("rb") as stream:
        return "shell" if _BASH.match(stream.readline(256)) else None


@functools.cache
def _parser(kind: str) -> Any:
    tree_sitter = importlib.import_module("tree_sitter")
    grammar = importlib.import_module(_GRAMMARS[kind])
    return tree_sitter.Parser(tree_sitter.Language(grammar.language()))


def _comments(root: Any) -> Iterator[Any]:
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "comment":
            yield node
        stack.extend(reversed(node.children))


def _text(node: Any) -> str:
    text = node.text.decode(errors="replace")
    if text.startswith("/*"):
        text = text[2:-2].lstrip("*")
    return text.lstrip("#/").strip()


def _shebang(node: Any) -> bool:
    return node.start_byte == 0 and node.text.startswith(b"#!")


def _documents(node: Any, source: bytes) -> bool:
    line = source[source.rfind(b"\n", 0, node.start_byte) + 1 : node.start_byte]
    following = node.next_named_sibling
    if not node.text.startswith(b"/**") or node.text == b"/**/" or line.strip() or following is None:
        return False
    if following.start_point[0] != node.end_point[0] + 1:
        return False
    if following.type == "export_statement":
        following = following.child_by_field_name("declaration") or following
    if following.type in ("lexical_declaration", "variable_declaration"):
        values = [declarator.child_by_field_name("value") for declarator in following.named_children]
        return bool(values) and all(value is not None and value.type in _FUNCTIONS for value in values)
    return following.type in _DOCUMENTED


def _header(comments: list[Any], code_start: int) -> list[Any]:
    leading = [node for node in comments if node.end_byte <= code_start and not _shebang(node)]
    run = leading[:1]
    for node in leading[1:]:
        if node.start_point[0] != run[-1].end_point[0] + 1:
            break
        run.append(node)
    return run


def tree_violations(source: bytes, kind: str, name: str) -> list[tuple[int, int, str]]:
    """List the forbidden comments of a shell, JavaScript or CSS source parsed by its tree-sitter grammar."""
    root = _parser(kind).parse(source).root_node
    if root.has_error:
        return [(1, 1, f"{kind} source does not parse")]
    comments = list(_comments(root))
    header = _header(
        comments, min((child.start_byte for child in root.children if child.type != "comment"), default=len(source))
    )
    found = []
    if header and header[-1].end_point[0] - header[0].start_point[0] >= HEADER_LINES:
        found.append(
            (header[0].start_point[0] + 1, header[-1].end_point[0] + 1, f"file header exceeds {HEADER_LINES} lines")
        )
    heading = {node.start_byte for node in header}
    for node in comments:
        text = _text(node)
        if node.start_byte in heading or _shebang(node) or _TREE_DIRECTIVES[kind].fullmatch(text):
            continue
        if _annotation(name, text):
            continue
        if kind == "javascript" and _documents(node, source):
            continue
        found.append((node.start_point[0] + 1, node.end_point[0] + 1, "forbidden comment"))
    return sorted(found)


def _found(path: Path, name: str, kind: str) -> list[tuple[int, int, str]]:
    if kind == "python":
        with tokenize.open(path) as stream:
            return violations(stream.read(), name)
    return tree_violations(path.read_bytes(), kind, name)


def _registry(root: Path) -> set[str]:
    path = root / REGISTRY
    if not path.exists():
        return set()
    value = json.loads(path.read_text())
    if not isinstance(value, list) or any(not isinstance(name, str) for name in value):
        raise ValueError(f"{REGISTRY} must be a list of paths")
    if any(Path(name).is_absolute() or ".." in Path(name).parts for name in value):
        raise ValueError(f"{REGISTRY} must contain repository relative paths")
    return set(value)


def _changed(base: Path, head: Path) -> set[int]:
    result = subprocess.run(
        [
            "git",
            "diff",
            "--no-index",
            "--no-ext-diff",
            "--no-renames",
            "--unified=0",
            "--",
            str(base) if base.exists() else "/dev/null",
            str(head),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1) or result.stderr:
        raise ValueError(f"git diff failed for {head.name}: {result.stderr.strip()}")
    lines = set()
    for match in _HUNK.finditer(result.stdout):
        start, count = int(match[1]), int(match[2]) if match[2] is not None else 1
        lines.update(range(start, start + count))
    return lines


def grade(base: Path | None, head: Path, whole_tree: bool) -> list[str]:
    tracked = _tracked(head)
    current = _registry(head)
    protected = _registry(base) if base is not None else set()
    errors = [f"{REGISTRY}: removed comment free path {name}" for name in sorted(protected - current)]
    for name in sorted(current - set(tracked)):
        errors.append(f"{REGISTRY}: comment free path is not tracked: {name}")
    for name in sorted(current & set(tracked)):
        if language(head, name) is None:
            errors.append(f"{REGISTRY}: comment free path is not a graded file: {name}")
    for name in tracked:
        path = head / name
        if not path.is_file():
            if name.endswith(".py"):
                errors.append(f"{name}: tracked Python file is missing")
            continue
        previous = base / name if base is not None else None
        changed = previous is None or not previous.is_file() or previous.read_bytes() != path.read_bytes()
        if not whole_tree and not changed and name not in current - protected:
            continue
        kind = language(head, name)
        if kind is None:
            continue
        found = _found(path, name, kind)
        lines = _changed(previous, path) if previous is not None and changed else set()
        for start, end, reason in found:
            if whole_tree or name in current | protected or any(line in lines for line in range(start, end + 1)):
                errors.append(f"{name}:{start}: {reason}")
    return errors


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reject new comments and preserve comment free files."
    )
    parser.add_argument("--base", type=Path)
    parser.add_argument("--head", type=Path, default=Path.cwd())
    parser.add_argument("--all", action="store_true", dest="whole_tree")
    args = parser.parse_args(argv)
    if args.base is None and not args.whole_tree:
        parser.error("--base is required unless --all is set")
    try:
        errors = grade(args.base, args.head, args.whole_tree)
    except (OSError, ValueError, SyntaxError, ImportError, tokenize.TokenError, subprocess.CalledProcessError) as error:
        errors = [f"cannot grade comments: {error}"]
    for error in errors:
        print(f"::error::{error}")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
