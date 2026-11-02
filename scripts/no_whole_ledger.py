"""Refuse new whole ledger state reads in the swarm and tick packages against a shrink only allowlist.

A whole ledger state read is any reference to ``state`` on a ledger client: ``ledger.state``,
``self._ledger.state``, ``context.ledger.state`` or ``LedgerClient().state``, called or passed on.
Each graded file may hold at most as many as the base allowlist records for it.
"""

import argparse
import ast
import json
from pathlib import Path

from scripts.size_limits import GradeError, files

SCOPE = ("scripts/swarm/", "scripts/tick/")
ALLOWLIST = Path("tests/WHOLE_LEDGER_ALLOWLIST.json")
LEDGERS = ("ledger", "_ledger")
CLIENTS = ("LedgerClient",)


def _ledger(node: ast.expr) -> bool:
    """True when node names a ledger client."""
    if isinstance(node, ast.Name):
        return node.id in LEDGERS
    if isinstance(node, ast.Attribute):
        return node.attr in LEDGERS
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in CLIENTS


def reads(source: str) -> int:
    """The number of whole ledger state reads in source."""
    tree = ast.parse(source)
    return sum(
        isinstance(node, ast.Attribute) and node.attr == "state" and _ledger(node.value) for node in ast.walk(tree)
    )


def measure(tree: Path) -> dict[str, int]:
    """Whole ledger state reads per graded file of tree, only files holding any."""
    tree = tree.resolve()
    measured: dict[str, int] = {}
    for path in files(tree):
        name = path.relative_to(tree).as_posix()
        if not name.startswith(SCOPE):
            continue
        try:
            count = reads(path.read_text())
        except SyntaxError as exc:
            raise GradeError(f"cannot parse {name}: {exc}") from exc
        if count:
            measured[name] = count
    return measured


def load(tree: Path) -> dict[str, int] | None:
    """The allowlist recorded in tree, or nothing when it has none."""
    path = tree / ALLOWLIST
    return json.loads(path.read_text()) if path.is_file() else None


def write(tree: Path) -> None:
    """Record the measurement of tree as its allowlist."""
    (tree / ALLOWLIST).write_text(json.dumps(measure(tree), indent=1) + "\n")


def grade(base: dict[str, int] | None, head: dict[str, int], recorded: dict[str, int] | None) -> list[str]:
    """Reject a missing base allowlist, reads above the base allowance and a head record that differs from its measurement."""
    if base is None:
        return [f"{ALLOWLIST} is missing from the base"]
    errors = [
        f"{name} reads the whole ledger state {count} times; the base allowlist allows {base.get(name, 0)}"
        for name, count in sorted(head.items())
        if count > base.get(name, 0)
    ]
    if recorded != head:
        errors.append(f"{ALLOWLIST} differs from the head's measurement; run python -m scripts.no_whole_ledger --write")
    return errors


def main(argv: list[str] | None = None) -> int:
    """Grade the head against the base, or record the head's allowlist with --write."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--base")
    parser.add_argument("--head", default=".")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    if not (args.write or args.base):
        parser.error("grading needs --base")
    tree = Path(args.head)
    try:
        if args.write:
            write(tree)
            return 0
        head, recorded, base = measure(tree), load(tree), load(Path(args.base))
    except (GradeError, ValueError, OSError) as exc:
        print(f"::error::{exc}")
        return 1
    errors = grade(base, head, recorded)
    print(
        f"{len(head)} files read the whole ledger state, {len(base or {})} on the base allowlist, {len(errors)} errors"
    )
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
