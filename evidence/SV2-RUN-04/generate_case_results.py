from pathlib import Path

from scripts.swarm_v2.case_results import write
from tests.sv2_run04_cases import case_a, case_b, case_c

ROOT = Path(__file__).resolve().parents[2]
INPUTS = (
    "scripts/swarm_v2/runtime/observe.py",
    "scripts/swarm/status.py",
    "tests/test_swarm_v2_observe.py",
    "tests/sv2_run04_cases.py",
    "evidence/SV2-RUN-04/generate_case_results.py",
    "Swarm-v2.md",
)
EVIDENCE_CLASS = "mocked signals and isolated fakeredis; no live Kubernetes, SSH or rollout proof"

if __name__ == "__main__":
    passed = write(ROOT, Path(__file__).parent, INPUTS, EVIDENCE_CLASS, (("a", case_a), ("b", case_b), ("c", case_c)))
    print(passed)
    raise SystemExit(0 if passed and all(passed.values()) else 1)
