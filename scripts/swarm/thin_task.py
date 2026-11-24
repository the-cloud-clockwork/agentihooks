"""The thin task check: which rules an open task's text misses before an engineer may claim it, and the grader of the
enhancement plan section a planner writes for a thin task. Pure functions over task records and plan text."""

import re
from typing import Any

from scripts.swarm.slice_check import CODE_KINDS, done_when, short
from scripts.swarm_ledger import ledger_kinds

__all__ = ["ENHANCE", "THIN_WORDS", "exempt", "section_complete", "thin"]

THIN_WORDS = 40
ENHANCE = "enhance-{}"
EXEMPT = ("planned", "skipped")
NAMED = re.compile(
    r"\b[A-Za-z]\w*\("
    r"|\b[a-z][a-z0-9]+(?:[._][a-z0-9]+)+"
    r"|(?<![\w/])/[\w{}-]+(?:/[\w{}-]+)*"
    r"|\b(?:agentihooks|python -m)\s+[a-z]"
    r"|\b(?:command|function|file)\s+(?:`[A-Za-z][\w./-]*`|[A-Za-z][\w./-]*)"
    r"|\bcommand\s+`[A-Za-z][\w./-]*(?:\s+[^`\n]+)?`",
    re.I,
)
OBSERVED = re.compile(
    r"\b(?:returns?|responds?|emits?|prints?|writes?|contains?|exits?|rejects?|raises?)\s+\S"
    r"|\b(?:has|have)\s+status\s+\S"
    r"|(?<=\))\s*(?:==|!=|<=|>=|<|>)\s*\S",
    re.I,
)
RECON = (
    "What will be built",
    "Code areas touched",
    "What might break",
    "Regression tests",
    "Resources needed",
    "Protection plan",
)
BODY = ("Summary", "Seams", "Tests", "Proof")


def thin(task: dict[str, Any]) -> list[str]:
    """Every rule the task fails; code and CI seams name a public boundary and observed outcome together."""
    if exempt(task):
        return []
    kind: str = ledger_kinds.kind(task)  # pyright: ignore[reportUnknownMemberType]
    tid, description = task["id"], task["description"]
    problems: list[str] = []
    if short(description, THIN_WORDS):
        problems.append(f"Task {tid} has a description under {THIN_WORDS} words.")
    if not done_when(description):
        problems.append(f"Task {tid} has no done when sentence.")
    if not task.get("territory"):
        problems.append(f"Task {tid} names no territory.")
    if kind in CODE_KINDS and not _named_seam(description):
        problems.append(f"Task {tid} is {kind} work that names no seam.")
    return problems


def _named_seam(text: str) -> bool:
    parts = re.split(r"(?<=[.!?])\s+|\n(?=\s*(?:[-*+]|\d+[.)]|#{1,6})\s)|\n\s*\n", text)
    return any(
        boundary.end() < observation.start() or observation.end() < boundary.start()
        for part in parts
        for boundary in NAMED.finditer(part)
        for observation in OBSERVED.finditer(part)
    )


def exempt(task: dict[str, Any]) -> bool:
    """Whether the task is never thin: it carries a plan slice, is plan kind work, or its enhancement is planned or
    skipped."""
    kind: str = ledger_kinds.kind(task)  # pyright: ignore[reportUnknownMemberType]
    return bool(task.get("plan_slice")) or kind == "plan" or task.get("enhancement") in EXEMPT


def section_complete(text: str) -> list[str]:
    """Every part an enhancement plan section misses: a boundary and observation under its Seams heading, each recon heading, each
    pull request body section and a done when sentence."""
    found = {name: _section(text, name) for name in (*RECON, *BODY)}
    seams = found["Seams"]
    problems = ["The section names no seam."] if seams is None or not _named_seam(seams) else []
    problems += [f"The section has no {name} heading." for name in RECON if found[name] is None]
    problems += [f"The section has no {name} pull request body section." for name in BODY if found[name] is None]
    if not done_when(text):
        problems.append("The section has no done when sentence.")
    return problems


def _section(text: str, name: str) -> str | None:
    found = re.search(rf"^#{{1,6}}[ \t]+{re.escape(name)}[ \t]*$(.*?)(?=^#|\Z)", text, re.I | re.M | re.S)
    return found.group(1) if found and found.group(1).strip() else None
