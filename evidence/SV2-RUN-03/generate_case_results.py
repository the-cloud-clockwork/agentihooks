from pathlib import Path

from scripts.swarm_v2.case_results import write
from tests.sv2_run03_cases import case_a, case_b, case_c

ROOT = Path(__file__).resolve().parents[2]
INPUTS = (
    "scripts/swarm_v2/runtime/operations.py",
    "scripts/swarm/store.py",
    "tests/test_swarm_v2_operations.py",
    "tests/sv2_run03_cases.py",
    "evidence/SV2-RUN-03/generate_case_results.py",
    "Swarm-v2.md",
)
EVIDENCE_CLASS = "mocked transport and isolated fakeredis; no live runtime or rollout proof"

if __name__ == "__main__":
    passed = write(ROOT, Path(__file__).parent, INPUTS, EVIDENCE_CLASS, (("a", case_a), ("b", case_b), ("c", case_c)))
    print(passed)
    raise SystemExit(0 if passed and all(passed.values()) else 1)
