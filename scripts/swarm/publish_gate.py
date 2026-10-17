import ast
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

NAME = "AgentiHooks touched Python fallback"
RULES = ("C901", "PLR0912", "PLR0913")
LIMITS = ("--config", "lint.mccabe.max-complexity=12", "--config", "lint.pylint.max-args=7")
_MEASURED = re.compile(r"\((\d+) > \d+\)")
_HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
_OLD = re.compile(r"@@ -(\d+)")
_SPEC = importlib.util.spec_from_file_location("size_limits", Path(__file__).resolve().parents[1] / "size_limits.py")
size_limits = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(size_limits)


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True)


def changed(base: str) -> dict[Path, Path]:
    fields = iter(git("diff", "--name-status", "-z", "-M", "--diff-filter=ACMRT", base, "HEAD").split("\0")[:-1])
    found = {}
    for status in fields:
        old = next(fields)
        new = next(fields) if status.startswith("R") else old
        if new.endswith(".py") and Path(new).is_file():
            found[Path(new)] = Path(old if old.endswith(".py") else new)
    return found


def changed_lines(base: str, path: Path, old: Path) -> set[int]:
    lines = set()
    for hunk in _HUNK.finditer(git("diff", "-U0", base, "--", old.as_posix(), path.as_posix())):
        start = int(hunk[1])
        lines.update(range(start, start + int(hunk[2] or 1)))
    return lines


def reformatted(diff: str) -> set[int]:
    rows, row = set(), 0
    for line in diff.splitlines():
        if hunk := _OLD.match(line):
            row = int(hunk[1])
        elif row and line.startswith("-"):
            rows.add(row)
            row += 1
        elif row and line.startswith("+"):
            rows.add(row)
        elif row:
            row += 1
    return rows


def lint(base: str, paths: dict[Path, Path]) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "ruff",
        "check",
        "--ignore-noqa",
        "--output-format",
        "json",
        "--",
        *map(str, paths),
    ]
    result = subprocess.run(command, stdout=subprocess.PIPE)
    if result.returncode > 1:
        return [f"ruff check exited {result.returncode}"]
    root, errors = Path.cwd().resolve(), []
    lines = {path.as_posix(): changed_lines(base, path, old) for path, old in paths.items()}
    for hit in json.loads(result.stdout):
        rel, row = Path(hit["filename"]).relative_to(root).as_posix(), hit["location"]["row"]
        if hit["code"] == "invalid-syntax" or lines[rel] & set(range(row, hit["end_location"]["row"] + 1)):
            errors.append(f"{rel}:{row}:{hit['location']['column']}: {hit['code']} {hit['message']}")
    if errors:
        return errors
    for path in paths:
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "format", "--diff", "--", str(path)], stdout=subprocess.PIPE
        )
        if result.returncode > 1:
            return [f"ruff format exited {result.returncode}"]
        if rows := reformatted(result.stdout.decode()) & lines[path.as_posix()]:
            errors.append(f"{path.as_posix()}:{min(rows)}: would reformat a changed line")
    return errors


def measure(tree: Path, paths: list[Path]) -> dict[tuple[str, str], tuple[int, str]]:
    found, defs = {}, {}
    for path in paths:
        source = (tree / path).read_bytes()
        defs[path.as_posix()] = size_limits.functions(source)
        if (lines := len(source.splitlines())) > 3000:
            found[(path.as_posix(), "file lines")] = (lines, f"{path}: file exceeds 3000 lines")
        for name, length in defs[path.as_posix()].values():
            if length > 300:
                found[(path.as_posix(), f"{name} lines")] = (length, f"{path}: {name} exceeds 300 lines")
    return found | complexity(tree, paths, defs)


def complexity(
    tree: Path, paths: list[Path], defs: dict[str, dict[int, tuple[str, int]]]
) -> dict[tuple[str, str], tuple[int, str]]:
    found = {}
    command = [sys.executable, "-m", "ruff", "check", "--isolated", "--ignore-noqa", "--output-format", "json"]
    command += ["--select", ",".join(RULES), *LIMITS, *(str(tree / path) for path in paths)]
    for hit in json.loads(subprocess.run(command, capture_output=True).stdout):
        rel = Path(hit["filename"]).relative_to(tree).as_posix()
        value = int(_MEASURED.search(hit["message"])[1])
        name = defs[rel][hit["location"]["row"]][0]
        found[(rel, f"{name} {hit['code']}")] = (value, f"{rel}: {name} {hit['code']} {hit['message']}")
    return found


def base_source(base: str, old: Path) -> bytes | None:
    shown = subprocess.run(["git", "show", f"{base}:{old.as_posix()}"], capture_output=True)
    try:
        ast.parse(shown.stdout)
    except (SyntaxError, ValueError):
        return None
    return shown.stdout if shown.returncode == 0 else None


def baseline(base: str, paths: dict[Path, Path]) -> dict[tuple[str, str], tuple[int, str]]:
    with tempfile.TemporaryDirectory() as directory:
        tree, present = Path(directory).resolve(), []
        for path, old in paths.items():
            if (content := base_source(base, old)) is not None:
                (tree / path).parent.mkdir(parents=True, exist_ok=True)
                (tree / path).write_bytes(content)
                present.append(path)
        return measure(tree, present) if present else {}


def logic(source: bytes) -> dict[str, str]:
    names = size_limits.functions(source)
    nodes = ast.walk(ast.parse(source))
    return {
        names[node.lineno][0]: ast.dump(node)
        for node in nodes
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def touched(base: str, paths: dict[Path, Path]) -> dict[str, set[str]]:
    found = {}
    for path, old in paths.items():
        before = {} if (content := base_source(base, old)) is None else logic(content)
        found[path.as_posix()] = {name for name, dump in logic(path.read_bytes()).items() if before.get(name) != dump}
    return found


def graded(
    head: dict[tuple[str, str], tuple[int, str]], names: dict[str, set[str]]
) -> dict[tuple[str, str], tuple[int, str]]:
    return {
        key: hit for key, hit in head.items() if key[1] == "file lines" or key[1].rsplit(" ", 1)[0] in names[key[0]]
    }


def allowance(
    base: str, measured: dict[tuple[str, str], tuple[int, str]], head: dict[tuple[str, str], tuple[int, str]]
) -> dict[tuple[str, str], tuple[int, str]]:
    shown = subprocess.run(["git", "show", f"{base}:{size_limits.ALLOWLIST.as_posix()}"], capture_output=True)
    listed = json.loads(shown.stdout) if shown.returncode == 0 else {}
    allowed = dict(measured)
    for (path, unit), (_, message) in head.items():
        name, rule = unit.rsplit(" ", 1)
        if unit == "file lines":
            limit = listed.get(path, {}).get("file-lines")
        else:
            limit = listed.get(f"{path}::{name}", {}).get("function-lines" if rule == "lines" else rule)
        if limit is None:
            continue
        if (path, unit) in allowed:
            limit = min(limit, allowed[(path, unit)][0])
        allowed[(path, unit)] = (limit, message)
    return allowed


def grade(base: dict[tuple[str, str], tuple[int, str]], head: dict[tuple[str, str], tuple[int, str]]) -> list[str]:
    errors = []
    for key, (value, message) in head.items():
        if key not in base:
            errors.append(message)
        elif value > base[key][0]:
            errors.append(f"{key[0]}: {key[1]} grew from {base[key][0]} to {value}")
    return errors


def main() -> int:
    print(f"{NAME}: {Path.cwd()}")
    base = git("merge-base", "origin/dev", "HEAD").strip()
    paths = changed(base)
    if paths:
        if errors := lint(base, paths):
            print("\n".join(errors))
            return 1
        head = graded(measure(Path.cwd().resolve(), list(paths)), touched(base, paths))
        if errors := grade(allowance(base, baseline(base, paths), head), head):
            print("\n".join(errors))
            return 1
    print(f"Graded {len(paths)} changed Python files")
    directory = Path(git("rev-parse", "--absolute-git-dir").strip())
    (directory / "ci_prepush").write_text(git("rev-parse", "HEAD"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
