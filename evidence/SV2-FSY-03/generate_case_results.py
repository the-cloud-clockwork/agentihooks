from pathlib import Path

from scripts.swarm_v2.case_results import write
from tests.sv2_fsy03_cases import case_a, case_b, case_c

ROOT = Path(__file__).resolve().parents[2]
INPUTS = (
    "scripts/swarm_v2/cache.py",
    "scripts/swarm_v2/filesystem.py",
    "docker/swarm-node/cache-policy.json",
    "docker/swarm-node/layout.json",
    "tests/test_swarm_v2_cache.py",
    "tests/sv2_fsy03_cases.py",
    "evidence/SV2-FSY-03/generate_case_results.py",
    "Swarm-v2.md",
)
EVIDENCE_CLASS = (
    "local isolated fixture: real files in temporary execution roots and a temporary cache store, "
    "fake clock and fake disk usage; no Pod, no shared volume and no live rollout proof"
)

if __name__ == "__main__":
    passed = write(ROOT, Path(__file__).parent, INPUTS, EVIDENCE_CLASS, (("a", case_a), ("b", case_b), ("c", case_c)))
    print(passed)
    raise SystemExit(0 if passed and all(passed.values()) else 1)
