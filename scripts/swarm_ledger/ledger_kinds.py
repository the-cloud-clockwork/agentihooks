"""Task kinds and their proof contracts: what a task of each kind must carry before it can be marked done."""

import re

KINDS = ("code", "ci", "ops", "troubleshoot", "tune", "research")
CONTRACT_KEYS = ("must", "check", "judge")
PROOF_KEYS = ("command", "output", "root_cause", "evidence", "fix", "filed", "finding")
LINK_RE = re.compile(r"^https?://[^\s]+$")
NEEDS = {
    "ops": ("command", "output"),
    "tune": ("command", "output"),
    "troubleshoot": ("root_cause", "evidence"),
    "research": ("finding",),
}


def kind(task):
    return task.get("kind") or "code"


def check(fields):
    if fields.get("kind", "code") not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    for key, allowed in (("contract", CONTRACT_KEYS), ("proof", PROOF_KEYS)):
        value = fields.get(key, {})
        if (
            not isinstance(value, dict)
            or set(value) - set(allowed)
            or not all(isinstance(v, str) for v in value.values())
        ):
            raise ValueError(f"{key} must be an object of strings with keys among {allowed}")


def unmet(task):
    """The proof fields a task still lacks before it may be marked done; empty when its contract is met."""
    proof = task.get("proof") or {}
    missing = [key for key in NEEDS.get(kind(task), ()) if not str(proof.get(key, "")).strip()]
    if kind(task) == "troubleshoot" and not any(str(proof.get(k, "")).strip() for k in ("fix", "filed")):
        missing.append("fix or filed")
    if kind(task) == "research" and "finding" not in missing and not LINK_RE.match(proof["finding"].strip()):
        missing.append("finding")
    return missing
