import ast
import subprocess
import sys
from pathlib import Path

NAME = "AgentiHooks touched Python fallback"


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True)


def changed() -> list[Path]:
    base = git("merge-base", "origin/dev", "HEAD").strip()
    names = git("diff", "--name-only", "-z", "--diff-filter=ACMRT", base, "HEAD").split("\0")
    return [Path(name) for name in names if name.endswith(".py") and Path(name).is_file()]


def lengths(paths: list[Path]) -> list[str]:
    errors = []
    for path in paths:
        source = path.read_text()
        if len(source.splitlines()) > 3000:
            errors.append(f"{path}: file exceeds 3000 lines")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.end_lineno - node.lineno + 1 > 300:
                errors.append(f"{path}: {node.name} exceeds 300 lines")
    return errors


def main() -> int:
    print(f"{NAME}: {Path.cwd()}")
    paths = changed()
    if paths:
        names = [str(path) for path in paths]
        commands = [
            ["check", "--", *names],
            ["format", "--check", "--", *names],
            [
                "check",
                "--isolated",
                "--select",
                "C901,PLR0912,PLR0913",
                "--config",
                "lint.mccabe.max-complexity=12",
                "--config",
                "lint.pylint.max-args=7",
                "--",
                *names,
            ],
        ]
        for command in commands:
            if code := subprocess.run([sys.executable, "-m", "ruff", *command]).returncode:
                return code
        if errors := lengths(paths):
            print("\n".join(errors))
            return 1
    print(f"Graded {len(paths)} changed Python files")
    directory = Path(git("rev-parse", "--absolute-git-dir").strip())
    (directory / "ci_prepush").write_text(git("rev-parse", "HEAD"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
