"""Task kinds and their proof contracts: what a task of each kind must carry before it can be marked done."""

import re

KINDS = ("code", "ci", "ops", "troubleshoot", "tune", "research", "plan")
BRANCHED = ("code", "ci")
CONTRACT_KEYS = ("must", "check", "judge")
CONTRACT_TIMES = ("after_at",)
CONTRACT_FLAGS = ("push", "after")
FLAG_VALUES = ("yes", "no")
PROOF_KEYS = ("command", "output", "root_cause", "evidence", "fix", "filed", "finding", "slice")
LINK_RE = re.compile(r"^https?://[^\s]+$")
NEEDS = {
    "plan": ("slice",),
    "ops": ("command", "output"),
    "tune": ("command", "output"),
    "troubleshoot": ("root_cause", "evidence"),
    "research": ("finding",),
}


def kind(task):
    return task.get("kind") or "code"


def after(task):
    return kind(task) in BRANCHED and (task.get("contract") or {}).get("after") == "yes"


def read_only(task):
    return kind(task) not in BRANCHED


def check(fields):
    if fields.get("kind", "code") not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    for key, allowed in (("contract", CONTRACT_KEYS + CONTRACT_TIMES + CONTRACT_FLAGS), ("proof", PROOF_KEYS)):
        value = fields.get(key, {})
        if (
            not isinstance(value, dict)
            or set(value) - set(allowed)
            or not all(isinstance(v, str) for v in value.values())
        ):
            raise ValueError(f"{key} must be an object of strings with keys among {allowed}")
    at = fields.get("contract", {}).get("after_at")
    if at and (not at.isascii() or not at.isdigit()):
        raise ValueError("contract after_at must be Unix milliseconds or empty")
    for flag in CONTRACT_FLAGS:
        if fields.get("contract", {}).get(flag, "no") not in FLAG_VALUES:
            raise ValueError(f"contract {flag} must be yes or no")


def unmet(task):
    """The proof fields a task still lacks before it may be marked done; empty when its contract is met."""
    proof = task.get("proof") or {}
    needs = ("command", "output") if after(task) else NEEDS.get(kind(task), ())
    missing = [key for key in needs if not str(proof.get(key, "")).strip()]
    if kind(task) == "troubleshoot" and not any(str(proof.get(k, "")).strip() for k in ("fix", "filed")):
        missing.append("fix or filed")
    if kind(task) == "research" and "finding" not in missing and not LINK_RE.match(proof["finding"].strip()):
        missing.append("finding")
    return missing
