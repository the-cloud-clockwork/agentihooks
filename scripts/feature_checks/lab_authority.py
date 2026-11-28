"""Live check of the lab authoritative static gates: this checkout lists lint, type-gate and test-count as lab
authoritative, keeps their authority through the exclusion catalog, declares the lab check that replaces each job
and skips each job behind the lab verdict, with the skip clause as one of the terms its condition joins with and,
and reads only commit id context names its static key carries. It prints one line per part and reads only files in
the checkout; a delivery file its reader refuses fails with one line."""

import json
import re
import sys
from pathlib import Path

import yaml

from scripts import ci_wiring
from scripts.ci_lab import delivery, static_gates

ROOT = Path(__file__).resolve().parents[2]
GATES = ("lint", "type-gate", "test-count")
WIRING = ".github/gate-wiring.json"
WORKFLOW = ".github/workflows/test.yml"
CLAUSE = "fromJSON(needs.lab-verdict.outputs.authoritative || '{{}}')['{gate}'] != true"
HELD = "the checkout holds {part}"
LACKS = "FAIL: the checkout lacks {part}"
UNREAD = "a delivery file its reader accepts: {error}"
COMMIT_ID_PART = "every gate of lint, type-gate and test-count reading only commit ids that its static key carries"
SHELL_SHA = re.compile(r"\bGITHUB_SHA\b|\bgit\s+rev-parse\b")


def _terms(condition: object) -> list[str]:
    """The terms a job condition joins with and, read without its expression braces: none when it joins any
    with or outside every pair of brackets, since such a condition can hold without them. A bracket inside
    quoted text counts as a bracket here; the protected grader, which holds a gate job to its base condition
    plus the exact lab clause, is the gate that refuses such a condition."""
    text = str(condition).removeprefix("${{").removesuffix("}}")
    depth = 0
    for index, mark in enumerate(text):
        depth += (mark == "(") - (mark == ")")
        if depth == 0 and text.startswith("||", index):
            return []
    return [term.strip() for term in text.split("&&")]


def _unlisted(gate: static_gates.StaticGate) -> list[str]:
    """Each context name the gate reads that names a commit id outside static_gates.COMMIT_ID_FIELDS: equal to
    github.sha or ending in sha, and not an earlier step's output, outcome or conclusion (steps.<id>.outputs.*
    is a produced value, shaped like static_gates.STEP_READ, never a commit id context of the grade)."""
    return [
        name
        for name in gate.reads
        if name not in static_gates.COMMIT_ID_FIELDS
        and name.endswith("sha")
        and static_gates.STEP_READ.fullmatch(name) is None
    ]


def _shell_reads(gate: static_gates.StaticGate) -> bool:
    """Whether the gate's own run text, or the run text of an earlier step whose output, outcome or conclusion it
    reads, names GITHUB_SHA or runs git rev-parse: a commit id read in the shell, invisible to expressions.reads
    and so to both the gate's reads and the static key. The match is on the text alone, so a script that only
    mentions either name, in an echoed message or a comparison, is refused too."""
    return any(SHELL_SHA.search(text) for text in (gate.run, *(producer.run for producer in gate.producers)))


def parts(root: Path, workflow_text: str) -> list[tuple[str, bool]]:
    """Each part of the feature with whether the checkout holds it: the list itself, then for each gate its kept
    authority, the lab check that replaces its job and the skip clause of that job, then whether every static gate
    of lint, type-gate and test-count, discovered from workflow_text with the reusable workflows at root, reads
    its commit ids only through names the static key carries and never through the shell. The input-key registry
    is not read: it sets the paths of a gate, never what the gate reads."""
    listed = list(delivery.lab(root)["authoritative"])
    kept = delivery.authoritative(root)
    checks = json.loads((root / WIRING).read_text())["lab_gates"]
    jobs = yaml.safe_load(workflow_text)["jobs"]
    found = [("exactly lint, type-gate and test-count in the lab authoritative list", listed == list(GATES))]
    for gate in GATES:
        job = jobs.get(gate, {})
        skipped = CLAUSE.format(gate=gate) in _terms(job.get("if")) and "lab-verdict" in job.get("needs", [])
        replaced = checks.get(f"lab/{gate}", {}).get("replaces") == f"test.yml/{gate}"
        found += [
            (f"lab authority for {gate}", gate in kept),
            (f"the lab check that replaces the {gate} job", replaced),
            (f"the lab skip clause of the {gate} job", skipped),
        ]
    gates = static_gates.discover(workflow_text, reusable=ci_wiring.load(root))
    scoped = [gate for gate in gates if gate.job.partition("/")[0] in GATES]
    unlisted = [name for gate in scoped for name in _unlisted(gate)]
    shelled = [gate.name for gate in scoped if _shell_reads(gate)]
    found.append((COMMIT_ID_PART, not unlisted and not shelled))
    return found


def main(root: Path | None = None) -> int:
    root = ROOT if root is None else root
    try:
        workflow_text = (root / WORKFLOW).read_text()
        found = parts(root, workflow_text)
    except delivery.DeliveryError as error:
        print(LACKS.format(part=UNREAD.format(error=error)))
        return 1
    for part, held in found:
        print((HELD if held else LACKS).format(part=part))
    return int(not all(held for _, held in found))


if __name__ == "__main__":
    sys.exit(main())
