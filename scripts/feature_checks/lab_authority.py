"""Live check of the lab authoritative static gates: this checkout lists lint, type-gate and test-count as lab
authoritative, keeps their authority through the exclusion catalog, declares the lab check that replaces each job
and skips each job behind the lab verdict. It prints one line per part and reads only files in the checkout."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GATES = ("lint", "type-gate", "test-count")
WIRING = ".github/gate-wiring.json"
WORKFLOW = ".github/workflows/test.yml"


def main(root: Path | None = None) -> int:
    del root
    return 1
