from pathlib import Path

from scripts.swarm_v2.case_results import write
from tests.sv2_fsy04_cases import case_a, case_b, case_c

ROOT = Path(__file__).resolve().parents[2]
INPUTS = (
    "scripts/swarm_v2/artifacts/base.py",
    "scripts/swarm_v2/artifacts/local.py",
    "scripts/swarm_v2/artifacts/object_store.py",
    "tests/test_swarm_v2_artifacts.py",
    "tests/sv2_fsy04_cases.py",
    "evidence/SV2-FSY-04/generate_case_results.py",
    "Swarm-v2.md",
)
EVIDENCE_CLASS = (
    "mocked: a local durable directory in a temporary root and an in memory S3 shaped object store fake "
    "with injected truncated uploads; no real object store, no Pod and no live rollout proof"
)

if __name__ == "__main__":
    passed = write(ROOT, Path(__file__).parent, INPUTS, EVIDENCE_CLASS, (("a", case_a), ("b", case_b), ("c", case_c)))
    print(passed)
    raise SystemExit(0 if passed and all(passed.values()) else 1)
