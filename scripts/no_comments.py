"""Grade Python comments and string statements against a merge base tree."""

import argparse
import ast
import io
import json
import re
import subprocess
import tokenize
from collections.abc import Sequence
from pathlib import Path

REGISTRY = "tests/COMMENT_FREE_PATHS.json"
_CODES = r"[A-Z]+\d+(?:\s*,\s*[A-Z]+\d+)*"
_NAMES = r"[\w.-]+(?:\s*,\s*[\w.-]+)*"
_DIRECTIVES = (
    rf"noqa(?::\s*{_CODES})?",
    rf"type:\s*ignore(?:\[{_NAMES}\])?",
    r"pragma:\s*no (?:cover|branch)",
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


def _tracked(root: Path) -> list[str]:
    result = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True)
    return sorted(set(result.stdout.decode().split("\0")) - {""})


def _directive(token: tokenize.TokenInfo, name: str) -> bool:
    text = token.string[1:].strip()
    if token.start == (1, 0) and text.startswith("!"):
        return True
    if token.start[0] <= 2 and token.start[1] == 0 and _COOKIE.fullmatch(text):
        return True
    if any(part.startswith(".semgrep") for part in Path(name).parts[:-1]):
        if re.fullmatch(rf"(?:ruleid|ok|todoruleid|todook):\s*{_NAMES}", text):
            return True
    return _DIRECTIVE.fullmatch(text) is not None


def violations(source: str, name: str) -> list[tuple[int, int, str]]:
    tree = ast.parse(source, filename=name)
    allowed = set()
    found = []
    for owner in ast.walk(tree):
        if not isinstance(owner, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not owner.body:
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


def _registry(root: Path) -> set[str]:
    path = root / REGISTRY
    if not path.exists():
        return set()
    value = json.loads(path.read_text())
    if not isinstance(value, list) or any(not isinstance(name, str) for name in value):
        raise ValueError(f"{REGISTRY} must be a list of paths")
    if any(Path(name).is_absolute() or ".." in Path(name).parts or not name.endswith(".py") for name in value):
        raise ValueError(f"{REGISTRY} must contain repository relative Python paths")
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
    if result.returncode not in (0, 1):
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
    for name in tracked:
        path = head / name
        if not path.is_file():
            if name.endswith(".py"):
                errors.append(f"{name}: tracked Python file is missing")
            continue
        if not name.endswith(".py"):
            continue
        previous = base / name if base is not None else None
        changed = previous is None or not previous.is_file() or previous.read_bytes() != path.read_bytes()
        if not whole_tree and not changed and name not in current - protected:
            continue
        with tokenize.open(path) as stream:
            found = violations(stream.read(), name)
        lines = _changed(previous, path) if previous is not None and changed else set()
        for start, end, reason in found:
            if whole_tree or name in current | protected or any(line in lines for line in range(start, end + 1)):
                errors.append(f"{name}:{start}: {reason}")
    return errors


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reject new Python comments and preserve comment free files.")
    parser.add_argument("--base", type=Path)
    parser.add_argument("--head", type=Path, default=Path.cwd())
    parser.add_argument("--all", action="store_true", dest="whole_tree")
    args = parser.parse_args(argv)
    if args.base is None and not args.whole_tree:
        parser.error("--base is required unless --all is set")
    try:
        errors = grade(args.base, args.head, args.whole_tree)
    except (OSError, ValueError, SyntaxError, tokenize.TokenError, subprocess.CalledProcessError) as error:
        errors = [f"cannot grade Python comments: {error}"]
    for error in errors:
        print(f"::error::{error}")
    return int(bool(errors))

# planted comment


if __name__ == "__main__":
    raise SystemExit(main())
