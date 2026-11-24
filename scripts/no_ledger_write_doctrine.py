"""Refuse new prose that treats the ledger as writable, against a shrink only allowlist.

The ledger web page is read only, a presentation layer for the operator (agentihooks PR #11561).
The one write path is the API route code, reachable as `agentihooks api` or, kept for
compatibility, `agentihooks ledger`. A skill, rule, role doc, doc page or spawn prompt that calls
a ledger write command teaches the wrong mental model: it is graded here, never by a code comment.
"""

import re
from pathlib import Path

from scripts.shrink_only_gate import load_allowlist
from scripts.shrink_only_gate import main as shrink_only_main
from scripts.size_limits import GradeError

SCOPE = ("profiles/", "docs/")
EXTRA_FILES = (
    "scripts/swarm/prompt.py",
    "scripts/swarm/dispatcher.py",
    "scripts/swarm/profile_choice.py",
    "scripts/swarm/claim_hold.py",
    "scripts/swarm/ledger_events.py",
    "scripts/gates/build.py",
    "scripts/gates/claims.py",
    "hooks/context/conditions.py",
    "hooks/context/context_recycle.py",
)
EXCLUDE_PREFIXES = ("profiles/package/skills/triage-priorities/scripts/",)
EXTENSIONS = (".md", ".py")
ALLOWLIST = Path("tests/LEDGER_WRITE_DOCTRINE_ALLOWLIST.json")
WRITE_VERBS = (
    "task add",
    "task set",
    "comment",
    "followup",
    "relay",
    "answer",
    "join",
    "leave",
    "ack",
    "say",
    "alert",
    "priority",
    "phase",
    "publish-plan",
    "artifact",
    "question",
    "scope",
    "retext",
    "edit",
    "delete",
    "fold",
    "time-left",
    "claim",
    "plan-backfill",
    "order",
)
FLAG = r"(?:--slug\s+\S+|--as\s+\S+)"
PATTERN = re.compile(
    r"agentihooks ledger\b(?:\s+" + FLAG + r")*\s+(?:" + "|".join(re.escape(v) for v in WRITE_VERBS) + r")\b"
)


def hits(text: str) -> int:
    """The number of ledger write doctrine lines in text."""
    return sum(1 for line in text.splitlines() if PATTERN.search(line))


def candidates(tree: Path) -> list[Path]:
    """Every prose or prompt file in scope under tree, in scanning order."""
    found = [tree / name for name in EXTRA_FILES if (tree / name).is_file()]
    for prefix in SCOPE:
        for path in sorted((tree / prefix).rglob("*")):
            rel = path.relative_to(tree).as_posix()
            if path.is_file() and path.suffix in EXTENSIONS and not rel.startswith(EXCLUDE_PREFIXES):
                found.append(path)
    return found


def measure(tree: Path) -> dict[str, int]:
    """Ledger write doctrine hits per in-scope file of tree, only files holding any, sorted by name."""
    tree = tree.resolve()
    measured: dict[str, int] = {}
    for path in candidates(tree):
        name = path.relative_to(tree).as_posix()
        try:
            count = hits(path.read_text())
        except (UnicodeDecodeError, OSError) as exc:
            raise GradeError(f"cannot read {name}: {exc}") from exc
        if count:
            measured[name] = count
    return dict(sorted(measured.items()))


def load(tree: Path) -> dict[str, int] | None:
    """The allowlist recorded in tree, or nothing when it has none."""
    return load_allowlist(tree, ALLOWLIST)


def grade(base: dict[str, int] | None, head: dict[str, int], recorded: dict[str, int] | None) -> list[str]:
    """Reject hits above the base allowance (an absent base allows none) and a head record that differs from its measurement."""
    allowed = base or {}
    errors = [
        f"{name} teaches a ledger write {count} times; the base allowlist allows {allowed.get(name, 0)}"
        for name, count in sorted(head.items())
        if count > allowed.get(name, 0)
    ]
    if recorded != head:
        errors.append(
            f"{ALLOWLIST} differs from the head's measurement; run python -m scripts.no_ledger_write_doctrine --write"
        )
    return errors


def main(argv: list[str] | None = None) -> int:
    """Grade the head against the base, or record the head's allowlist with --write."""
    return shrink_only_main(argv, ALLOWLIST, measure, grade, "teach a ledger write")


if __name__ == "__main__":
    raise SystemExit(main())
