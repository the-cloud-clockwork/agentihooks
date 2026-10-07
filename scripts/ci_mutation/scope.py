import ast
import re
import subprocess
from pathlib import Path


def changed_lines(diff: str) -> set[int]:
    lines = set()
    for match in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", diff, re.MULTILINE):
        start = int(match[1])
        count = int(match[2]) if match[2] is not None else 1
        lines.update(range(start, start + count))
    return lines


def discover_changes(root: Path, base: str, head: str) -> dict[str, set[int]]:
    comparison = f"{base}...{head}"
    names = (
        subprocess.check_output(
            ["git", "diff", "--name-only", "-z", comparison, "--", "hooks/", "scripts/"],
            cwd=root,
        )
        .decode()
        .split("\0")
    )
    changes = {}
    for name in names:
        path = Path(name)
        if path.suffix != ".py" or not (root / path).is_file():
            continue
        diff = subprocess.check_output(["git", "diff", "-U0", comparison, "--", name], cwd=root).decode()
        changes[name] = changed_lines(diff)
    return changes


def select_tests(root: Path, source: Path) -> list[str]:
    parts = source.with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    module = ".".join(parts)
    parent, _, name = module.rpartition(".")
    selected = []
    for path in sorted((root / "tests").rglob("test_*.py")):
        text = path.read_text()
        nodes = ast.walk(ast.parse(text))
        imports = any(
            isinstance(node, ast.ImportFrom)
            and (node.module == module or node.module == parent and any(alias.name == name for alias in node.names))
            for node in nodes
        )
        if path.name == f"test_{source.stem}.py" or module in text or imports:
            selected.append(path.relative_to(root).as_posix())
    return selected
