import ast
import json
import re
import subprocess
import sys
from pathlib import Path

NAME = "AgentiHooks touched Python fallback"
ALLOWLIST = "tests/SIZE_ALLOWLIST.json"
RULES = ("C901", "PLR0912", "PLR0913")
SETTINGS = ("lint.mccabe.max-complexity=12", "lint.pylint.max-branches=12", "lint.pylint.max-args=7")
FUNCTION_LINES = 300
FILE_LINES = 3000
MEASURED = re.compile(r"\((\d+) > \d+\)")


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True)


def show(base: str, name: str) -> str | None:
    result = subprocess.run(["git", "show", f"{base}:{name}"], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def changed(base: str) -> list[Path]:
    names = git("diff", "--name-only", "-z", "--diff-filter=ACMRT", base, "HEAD").split("\0")
    return [Path(name) for name in names if name.endswith(".py") and Path(name).is_file()]


def units(source: str) -> dict[str, ast.AST]:
    found, seen = {}, {}

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                seen[name] = seen.get(name, 0) + 1
                if seen[name] > 1:
                    name = f"{name}#{seen[name]}"
                if not isinstance(child, ast.ClassDef):
                    found[name] = child
                walk(child, f"{name}.")
            else:
                walk(child, prefix)

    walk(ast.parse(source), "")
    return found


def touched(path: Path, base: str) -> dict[int, tuple[str, int]]:
    try:
        old = units(show(base, path.as_posix()) or "")
    except SyntaxError:
        old = {}
    return {
        node.lineno: (name, node.end_lineno - node.lineno + 1)
        for name, node in units(path.read_text()).items()
        if name not in old or ast.dump(old[name]) != ast.dump(node)
    }


def measure(paths: list[Path], base: str) -> dict[str, dict[str, int]]:
    graded = {path: touched(path, base) for path in paths}
    measured = {}
    for path, rows in graded.items():
        if (lines := len(path.read_text().splitlines())) > FILE_LINES:
            measured.setdefault(path.as_posix(), {})["file-lines"] = lines
        for name, length in rows.values():
            if length > FUNCTION_LINES:
                measured.setdefault(f"{path.as_posix()}::{name}", {})["function-lines"] = length
    command = [sys.executable, "-m", "ruff", "check", "--isolated", "--exit-zero", "--output-format", "json"]
    command += ["--select", ",".join(RULES), *(f"--config={setting}" for setting in SETTINGS), "--"]
    output = subprocess.run([*command, *map(str, paths)], capture_output=True, text=True, check=True).stdout
    root = Path.cwd().resolve()
    for hit in json.loads(output):
        path, row = Path(hit["filename"]).resolve().relative_to(root), hit["location"]["row"]
        if row in graded.get(path, {}) and (value := MEASURED.search(hit["message"])):
            measured.setdefault(f"{path.as_posix()}::{graded[path][row][0]}", {})[hit["code"]] = int(value[1])
    return measured


def grade(measured: dict[str, dict[str, int]], allowed: dict[str, dict[str, int]]) -> list[str]:
    errors = []
    for unit, rules in sorted(measured.items()):
        for rule, value in sorted(rules.items()):
            limit = allowed.get(unit, {}).get(rule)
            if limit is None:
                errors.append(f"{unit} breaks {rule} at {value}; it is not on the base allowlist")
            elif value > limit:
                errors.append(f"{unit} grew on {rule} from {limit} to {value}")
    return errors


def main() -> int:
    print(f"{NAME}: {Path.cwd()}")
    base = git("merge-base", "origin/dev", "HEAD").strip()
    paths = changed(base)
    if paths:
        names = [str(path) for path in paths]
        for command in (["check", "--", *names], ["format", "--check", "--", *names]):
            if code := subprocess.run([sys.executable, "-m", "ruff", *command]).returncode:
                return code
        if errors := grade(measure(paths, base), json.loads(show(base, ALLOWLIST) or "{}")):
            print("\n".join(errors))
            return 1
    print(f"Graded {len(paths)} changed Python files")
    directory = Path(git("rev-parse", "--absolute-git-dir").strip())
    (directory / "ci_prepush").write_text(git("rev-parse", "HEAD"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
