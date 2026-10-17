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
    command = [sys.executable, "-m", "ruff", "check", "--isolated", "--output-format", "json"]
    command += ["--select", ",".join(RULES), *LIMITS, *(str(tree / path) for path in paths)]
    for hit in json.loads(subprocess.run(command, capture_output=True).stdout):
        rel = Path(hit["filename"]).relative_to(tree).as_posix()
        value = int(_MEASURED.search(hit["message"])[1])
        name = defs[rel][hit["location"]["row"]][0]
        found[(rel, f"{name} {hit['code']}")] = (value, f"{rel}: {name} {hit['code']} {hit['message']}")
    return found


def baseline(base: str, paths: dict[Path, Path]) -> dict[tuple[str, str], tuple[int, str]]:
    with tempfile.TemporaryDirectory() as directory:
        tree, present = Path(directory).resolve(), []
        for path, old in paths.items():
            shown = subprocess.run(["git", "show", f"{base}:{old.as_posix()}"], capture_output=True)
            try:
                ast.parse(shown.stdout)
            except (SyntaxError, ValueError):
                continue
            if shown.returncode == 0:
                (tree / path).parent.mkdir(parents=True, exist_ok=True)
                (tree / path).write_bytes(shown.stdout)
                present.append(path)
        return measure(tree, present) if present else {}


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
        names = [str(path) for path in paths]
        for command in (["check", "--", *names], ["format", "--check", "--", *names]):
            if code := subprocess.run([sys.executable, "-m", "ruff", *command]).returncode:
                return code
        if errors := grade(baseline(base, paths), measure(Path.cwd().resolve(), list(paths))):
            print("\n".join(errors))
            return 1
    print(f"Graded {len(paths)} changed Python files")
    directory = Path(git("rev-parse", "--absolute-git-dir").strip())
    (directory / "ci_prepush").write_text(git("rev-parse", "HEAD"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
