import argparse
import ast
import os
import re
import subprocess
from pathlib import Path

from scripts.ci_mutation.scope import imported_names

REPLAYS = ("tests/swarm_ledger/repository_replay.py", "tests/swarm_ledger/page_replay.py")
# The replays put the ledger folder ahead of the checkout root on sys.path.
SEARCH = ("scripts/swarm_ledger", ".")
# The replays read these without importing them: the served page, their recorded fixtures and this workflow.
# A change to this trigger or its graph helper runs the check, so a pull request cannot skip it by editing them.
READ = (
    "scripts/swarm_ledger/",
    "tests/swarm_ledger/",
    ".github/workflows/equivalence.yml",
    "scripts/ci_equivalence_trigger.py",
    "scripts/ci_mutation/scope.py",
    "Swarm-v2.md",
)
DOTTED = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")


def module_files(root: Path, name: str) -> list[Path]:
    parts = name.split(".")
    found = []
    for base in SEARCH:
        for depth in range(1, len(parts) + 1):
            stem = (root / base).joinpath(*parts[:depth])
            found += [path for path in (stem / "__init__.py", stem.with_suffix(".py")) if path.is_file()]
    return found


def patched_names(text: str) -> set[str]:
    return {
        node.value
        for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and DOTTED.fullmatch(node.value)
    }


def imported_files(root: Path) -> set[str]:
    pending = [root / replay for replay in REPLAYS]
    for replay in pending.copy():
        for name in patched_names(replay.read_text()):
            pending += module_files(root, name)
    seen = set()
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        package = path.relative_to(root).parent.parts
        for name in imported_names(path.read_text(), package):
            pending += module_files(root, name)
    return {path.relative_to(root).as_posix() for path in seen}


def triggering(root: Path, changed: list[str], files: set[str]) -> list[str]:
    # A deleted module is absent from the head graph even when an unchanged importer still names it.
    return [
        path
        for path in changed
        if path in files or path.startswith(READ) or (path.endswith(".py") and not (root / path).exists())
    ]


def changed_files(root: Path, base: str, head: str) -> list[str]:
    command = ["git", "diff", "--name-only", "--no-renames", "-z", f"{base}...{head}"]
    return [name for name in subprocess.check_output(command, cwd=root).decode().split("\0") if name]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the ledger equivalence check when a change reaches its replays.")
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args(argv)
    root = Path.cwd().resolve()
    hits = triggering(root, changed_files(root, args.base, args.head), imported_files(root))
    for path in hits:
        print(f"reaches the replays: {path}")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"run={'true' if hits else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
