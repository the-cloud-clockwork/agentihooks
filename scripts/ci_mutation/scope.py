import ast
import os
import re
import subprocess
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

    def keeps_tests(sha: str) -> bool:
        counts = git("diff", "--numstat", sha, head, "--", "tests").splitlines()
        return all(line.split("\t")[1] == "0" for line in counts)

    tip = git("rev-parse", head)
    bases = {git("merge-base", base, head)}
    if git("for-each-ref", INTEGRATION):
        bases.add(git("merge-base", INTEGRATION, head))
    unmerged = [f"--no-merged={own}" for own in bases]
    pushed = git("for-each-ref", "--merged", head, *unmerged, "--format=%(objectname)", "refs/remotes/origin").split()
    bases.update(sha for sha in set(pushed) - {tip} if keeps_tests(sha) and graded(sha))
    return sorted(bases)


def graded_green(sha: str) -> bool:
    def suites(check: str) -> set[str]:
        found = subprocess.run(
            [
                "gh",
                "api",
                "-X",
                "GET",
                f"repos/{os.environ['GITHUB_REPOSITORY']}/commits/{sha}/check-runs",
                "-f",
                f"check_name={check}",
                "-f",
                "status=completed",
                "--jq",
                '.check_runs[] | select(.conclusion == "success") | .check_suite.id',
            ],
            capture_output=True,
            text=True,
        )
        return set(found.stdout.split()) if found.returncode == 0 else set()

    return bool(suites("mutation") or suites("Gate — Required") & suites("mutation (0)"))


def discover_changes(root: Path, base: str | list[str], head: str) -> dict[str, set[int]]:
    bases = own_bases(root, base, head) if isinstance(base, str) else base
    found = [changes_since(root, own, head) for own in bases]
    return {
        name: set.intersection(*(changes[name] for changes in found))
        for name in found[0]
        if all(name in changes for changes in found)
    }


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
