from pathlib import Path

from scripts.swarm_v2.case_results import write
from tests.sv2_fsy02_cases import case_a, case_b, case_c

ROOT = Path(__file__).resolve().parents[2]
INPUTS = (
    "scripts/swarm_v2/workspaces.py",
    "scripts/swarm_v2/filesystem.py",
    "scripts/swarm/naming.py",
    "docker/swarm-node/layout.json",
    "tests/test_swarm_v2_workspaces.py",
    "tests/sv2_fsy02_cases.py",
    "evidence/SV2-FSY-02/generate_case_results.py",
    "Swarm-v2.md",
)
EVIDENCE_CLASS = (
    "local isolated fixture: real git against file origins in temporary execution roots; "
    "no network remote, no Pod and no live rollout proof"
)

if __name__ == "__main__":
    passed = write(ROOT, Path(__file__).parent, INPUTS, EVIDENCE_CLASS, (("a", case_a), ("b", case_b), ("c", case_c)))
    print(passed)
    raise SystemExit(0 if passed and all(passed.values()) else 1)
