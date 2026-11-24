"""Refuse new whole ledger state reads in the swarm and tick packages against a shrink only allowlist.

A whole ledger state read is any reference to ``state`` on a ledger client: ``ledger.state``,
``self._ledger.state``, ``context.ledger.state`` or ``LedgerClient().state``, called or passed on.
Each graded file may hold at most as many as the base allowlist records for it.
"""

import ast
from pathlib import Path

from scripts.shrink_only_gate import load_allowlist
from scripts.shrink_only_gate import main as shrink_only_main
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
    return load_allowlist(tree, ALLOWLIST)


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
    return shrink_only_main(argv, ALLOWLIST, measure, grade, "read the whole ledger state")


if __name__ == "__main__":
    raise SystemExit(main())
