import ast
import re
import subprocess
from functools import cache
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


def module_name(path: Path) -> str:
    parts = path.with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


@cache
def imported_names(text: str, package: tuple[str, ...]) -> frozenset[str]:
    names = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else ()
            module = ".".join([*base, *([node.module] if node.module else [])])
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
    return frozenset(names)


def select_tests(root: Path, source: Path) -> list[str]:
    module = module_name(source)
    named = re.compile(rf"(?<![\w.]){re.escape(module)}(?!\w)")
    modules = {}
    for path in sorted((root / "tests").rglob("*.py")):
        text = path.read_text()
        modules[path] = (text, imported_names(text, path.relative_to(root).parent.parts))
    reaching = {
        path
        for path, (text, imported) in modules.items()
        if module in imported
        or path.name.startswith("test_")
        and (path.name == f"test_{source.stem}.py" or named.search(text))
    }
    added = reaching
    while added:
        reached = {module_name(path.relative_to(root)) for path in added}
        added = {path for path, (_, imported) in modules.items() if path not in reaching and imported & reached}
        reaching |= added
    return [path.relative_to(root).as_posix() for path in modules if path in reaching and path.name.startswith("test_")]
