import ast
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


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True)


def changed(base: str) -> list[Path]:
    names = git("diff", "--name-only", "-z", "--diff-filter=ACMRT", base, "HEAD").split("\0")
    return [Path(name) for name in names if name.endswith(".py") and Path(name).is_file()]


def units(source: str) -> dict[int, tuple[str, int]]:
    found = {}

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{child.name}"
                if not isinstance(child, ast.ClassDef):
                    found[child.lineno] = (name, child.end_lineno - child.lineno + 1)
                walk(child, f"{name}.")
            else:
                walk(child, prefix)

    walk(ast.parse(source), "")
    return found


def measure(tree: Path, paths: list[Path]) -> dict[tuple[str, str], tuple[int, str]]:
    found, defs = {}, {}
    for path in paths:
        source = (tree / path).read_text()
        defs[path.as_posix()] = units(source)
        if (lines := len(source.splitlines())) > 3000:
            found[(path.as_posix(), "file lines")] = (lines, f"{path}: file exceeds 3000 lines")
        for name, length in defs[path.as_posix()].values():
            if length > 300:
                found[(path.as_posix(), f"{name} lines")] = (length, f"{path}: {name} exceeds 300 lines")
    command = [sys.executable, "-m", "ruff", "check", "--isolated", "--exit-zero", "--output-format", "json"]
    command += ["--select", ",".join(RULES), *LIMITS, "--", *(str(tree / path) for path in paths)]
    for hit in json.loads(subprocess.run(command, capture_output=True, text=True, check=True).stdout):
        rel = Path(hit["filename"]).relative_to(tree).as_posix()
        value = _MEASURED.search(hit["message"])
        if hit["code"] in RULES and value:
            name = defs[rel].get(hit["location"]["row"], (f"line {hit['location']['row']}",))[0]
            found[(rel, f"{name} {hit['code']}")] = (int(value[1]), f"{rel}: {name} {hit['code']} {hit['message']}")
    return found


def baseline(base: str, paths: list[Path]) -> dict[tuple[str, str], tuple[int, str]]:
    with tempfile.TemporaryDirectory() as directory:
        tree, present = Path(directory).resolve(), []
        for path in paths:
            shown = subprocess.run(["git", "show", f"{base}:{path.as_posix()}"], capture_output=True, text=True)
            if shown.returncode == 0:
                (tree / path).parent.mkdir(parents=True, exist_ok=True)
                (tree / path).write_text(shown.stdout)
                present.append(path)
        return measure(tree, present) if present else {}


def grade(base: dict, head: dict) -> list[str]:
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
        if errors := grade(baseline(base, paths), measure(Path.cwd().resolve(), paths)):
            print("\n".join(errors))
            return 1
    print(f"Graded {len(paths)} changed Python files")
    directory = Path(git("rev-parse", "--absolute-git-dir").strip())
    (directory / "ci_prepush").write_text(git("rev-parse", "HEAD"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
