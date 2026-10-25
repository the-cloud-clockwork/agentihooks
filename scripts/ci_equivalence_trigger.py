import argparse
import subprocess
from collections.abc import Iterable
from pathlib import Path

TRIGGERS = (
    "scripts/swarm_ledger/",
    "tests/swarm_ledger/",
    ".github/workflows/equivalence.yml",
    "scripts/ci_equivalence_trigger.py",
)


def changed(root: Path) -> list[str]:
    diff = ["git", "-C", str(root), "diff", "--no-renames", "--name-only", "HEAD^1", "HEAD"]
    return subprocess.run(diff, check=True, capture_output=True, text=True).stdout.splitlines()


def triggered(paths: Iterable[str]) -> bool:
    return any(path.startswith(TRIGGERS) for path in paths)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root")
    args = parser.parse_args(argv)
    print(f"run={str(triggered(changed(Path(args.root)))).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
