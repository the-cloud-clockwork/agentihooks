"""Live check of the lab authoritative static gates: this checkout lists lint, type-gate and test-count as lab
authoritative, keeps their authority through the exclusion catalog, declares the lab check that replaces each job
and skips each job behind the lab verdict, with the skip clause as one of the terms its condition joins with and.
It prints one line per part and reads only files in the checkout; a delivery file its reader refuses fails with
one line."""

import json
import sys
from pathlib import Path

import yaml

from scripts.ci_lab import delivery

ROOT = Path(__file__).resolve().parents[2]
GATES = ("lint", "type-gate", "test-count")
WIRING = ".github/gate-wiring.json"
WORKFLOW = ".github/workflows/test.yml"
CLAUSE = "fromJSON(needs.lab-verdict.outputs.authoritative || '{{}}')['{gate}'] != true"
HELD = "the checkout holds {part}"
LACKS = "FAIL: the checkout lacks {part}"
UNREAD = "a delivery file its reader accepts: {error}"


def _terms(condition: object) -> list[str]:
    """The terms a job condition joins with and, read without its expression braces: none when it joins any
    with or outside every pair of brackets, since such a condition can hold without them."""
    text = str(condition).removeprefix("${{").removesuffix("}}")
    depth = 0
    for index, mark in enumerate(text):
        depth += (mark == "(") - (mark == ")")
        if depth == 0 and text.startswith("||", index):
            return []
    return [term.strip() for term in text.split("&&")]


def parts(root: Path) -> list[tuple[str, bool]]:
    """Each part of the feature with whether the checkout holds it: the list itself, then for each gate its kept
    authority, the lab check that replaces its job and the skip clause of that job."""
    listed = list(delivery.lab(root)["authoritative"])
    kept = delivery.authoritative(root)
    checks = json.loads((root / WIRING).read_text())["lab_gates"]
    jobs = yaml.safe_load((root / WORKFLOW).read_text())["jobs"]
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
    return found


def main(root: Path | None = None) -> int:
    try:
        found = parts(ROOT if root is None else root)
    except delivery.DeliveryError as error:
        print(LACKS.format(part=UNREAD.format(error=error)))
        return 1
    for part, held in found:
        print((HELD if held else LACKS).format(part=part))
    return int(not all(held for _, held in found))


if __name__ == "__main__":
    sys.exit(main())
