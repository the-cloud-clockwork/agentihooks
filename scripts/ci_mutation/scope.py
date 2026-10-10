import ast
import os
import re
import subprocess
from collections import Counter
from functools import cache
from pathlib import Path

INTEGRATION = "refs/remotes/origin/dev"


def changed_lines(diff: str) -> set[int]:
    lines = set()
    for match in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", diff, re.MULTILINE):
        start = int(match[1])
        count = int(match[2]) if match[2] is not None else 1
        lines.update(range(start, start + count))
    return lines


def own_bases(root: Path, base: str, head: str, graded=lambda sha: False) -> list[str]:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=root).decode().strip()

    tip = git("rev-parse", head)
    given = git("merge-base", base, head)
    bases = {given}
    if git("for-each-ref", INTEGRATION):
        bases.add(git("merge-base", INTEGRATION, head))
    unmerged = [f"--no-merged={own}" for own in bases]
    pushed = git("for-each-ref", "--merged", head, *unmerged, "--format=%(objectname)", "refs/remotes/origin").split()
    bases.update(sha for sha in set(pushed) - {tip} if graded(sha))
    return [given, *sorted(bases - {given})]


def graded_green(sha: str) -> bool:
    found = subprocess.run(
        [
            "gh",
            "api",
            "-X",
            "GET",
            f"repos/{os.environ['GITHUB_REPOSITORY']}/commits/{sha}/check-runs",
            "-f",
            "check_name=mutation",
            "-f",
            "status=completed",
            "--jq",
            '[.check_runs[] | select(.conclusion == "success")] | length',
        ],
        capture_output=True,
        text=True,
    )
    return found.returncode == 0 and found.stdout.strip() not in ("", "0")


def discover_changes(root: Path, base: str | list[str], head: str) -> dict[str, set[int]]:
    bases = own_bases(root, base, head) if isinstance(base, str) else base
    given, *newer = [changes_since(root, own, head) for own in bases]
    weak = [weakened_tests(root, own, head) for own in bases[1:]]
    found = {}
    for name, lines in given.items():
        kept = [lines if tests else changes.get(name) for changes, tests in zip(newer, weak)]
        if None not in kept:
            found[name] = lines.intersection(*kept)
    return found


def is_test_file(path: str) -> bool:
    return Path(path).name.startswith("test_") and Path(path).suffix == ".py"


def weakened_tests(root: Path, base: str, head: str) -> set[str]:
    command = ["git", "diff", "--name-only", "--no-renames", "-z", base, head, "--", "tests"]
    names = subprocess.check_output(command, cwd=root).decode().split("\0")
    return {name for name in names if name and not adds_only_tests(root, base, head, name)}


def adds_only_tests(root: Path, base: str, head: str, name: str) -> bool:
    if not is_test_file(name):
        return False
    old, new = (blob(root, rev, name) for rev in (base, head))
    if new is None:
        return False
    previous = ast.parse(old or b"").body
    nodes = ast.parse(new).body
    before = Counter(ast.dump(node) for node in previous)
    after = Counter(ast.dump(node) for node in nodes)
    added = after - before
    taken = {bound for node in previous for bound in bindings(node)}
    fresh = Counter(bound for node in nodes for bound in bindings(node)) - Counter(taken)
    if before - after or any(bound in taken or count > 1 for bound, count in fresh.items()):
        return False
    return all(is_test(node) for node in nodes if added[ast.dump(node)])


def blob(root: Path, rev: str, name: str) -> bytes | None:
    shown = subprocess.run(["git", "show", f"{rev}:{name}"], cwd=root, capture_output=True)
    return shown.stdout if shown.returncode == 0 else None


def is_test(node: ast.stmt) -> bool:
    if isinstance(node, ast.Import | ast.ImportFrom):
        return True
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return node.name.startswith("test_") and marked(node)
    if isinstance(node, ast.ClassDef):
        methods = all(isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef) and is_test(item) for item in node.body)
        return node.name.startswith("Test") and marked(node) and methods
    return False


def marked(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> bool:
    return all(ast.unparse(mark).startswith("pytest.mark.") for mark in node.decorator_list)


def bindings(node: ast.stmt) -> list[str]:
    if isinstance(node, ast.Import | ast.ImportFrom):
        return [alias.asname or alias.name.split(".")[0] for alias in node.names]
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return [node.name]
    return []


def changes_since(root: Path, base: str, head: str) -> dict[str, set[int]]:
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
        if not path.is_file():
            continue
        text = path.read_text()
        modules[path] = (text, imported_names(text, path.relative_to(root).parent.parts))
    reaching = {
        path
        for path, (text, imported) in modules.items()
        if module in imported or named.search(text) or path.name == f"test_{source.stem}.py"
    }
    for _ in modules:
        reached = {module_name(path.relative_to(root)) for path in reaching}
        added = {path for path, (_, imported) in modules.items() if path not in reaching and imported & reached}
        if not added:
            break
        reaching |= added
    return [path.relative_to(root).as_posix() for path in modules if path in reaching and path.name.startswith("test_")]
