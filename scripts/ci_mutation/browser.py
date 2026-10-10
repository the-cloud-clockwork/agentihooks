import argparse
import ast
import os
from pathlib import Path

from scripts.ci_mutation.scope import discover_changes, select_tests


def imported_paths(root: Path, path: Path, nodes: list[ast.AST]) -> list[Path]:
    modules = []
    for node in nodes:
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = list(path.parent.relative_to(root).parts)
            if node.level:
                prefix = prefix[: len(prefix) - node.level + 1]
            else:
                prefix = []
            module = ".".join([*prefix, node.module or ""]).strip(".")
            modules.extend([module, *(f"{module}.{alias.name}" for alias in node.names)])
    paths = []
    for module in modules:
        target = root.joinpath(*module.split("."))
        paths.extend(
            candidate
            for candidate in (target.with_suffix(".py"), target / "__init__.py")
            if candidate.is_relative_to(root / "tests") and candidate.is_file()
        )
    return paths


def needs_browser(root: Path, tests: list[str]) -> bool:
    pending = [root / test for test in tests]
    for test in tests:
        parent = (root / test).parent
        while parent != root:
            pending.extend(path for path in (parent / "conftest.py", parent / "__init__.py") if path.is_file())
            parent = parent.parent
    seen = set()
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        nodes = list(ast.walk(ast.parse(path.read_text())))
        for node in nodes:
            if isinstance(node, ast.Import) and any(alias.name.split(".")[0] == "playwright" for alias in node.names):
                return True
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "playwright":
                return True
            if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
                if isinstance(node.args[0].value, str) and node.args[0].value.split(".")[0] == "playwright":
                    return True
        pending.extend(imported_paths(root, path, nodes))
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="origin/dev")
    parser.add_argument("--bases")
    args = parser.parse_args()
    root = Path.cwd()
    changes = discover_changes(root, args.bases.split(",") if args.bases is not None else args.base, "HEAD")
    tests = sorted({test for path in changes for test in select_tests(root, Path(path))})
    browser = str(needs_browser(root, tests)).lower()
    print(f"Selected mutation tests: {len(tests)}\nBrowser required: {browser}")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a") as stream:
            stream.write(f"browser={browser}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
