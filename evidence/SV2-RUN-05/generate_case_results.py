from pathlib import Path

from scripts.swarm_v2.case_results import write
from tests.sv2_run05_cases import case_a, case_b, case_c

ROOT = Path(__file__).resolve().parents[2]
INPUTS = (
    "scripts/swarm_v2/runtime/commands.py",
    "scripts/swarm_v2/runtime/operations.py",
    "tests/test_swarm_v2_commands.py",
    "tests/sv2_run05_cases.py",
    "tests/fixtures/swarm_v2/runtime-commands.json",
    "evidence/SV2-RUN-05/generate_case_results.py",
    "Swarm-v2.md",
)
EVIDENCE_CLASS = "isolated fakeredis and mocked command transports; no live terminal, Pod or UI rollout proof"

if __name__ == "__main__":
    passed = write(ROOT, Path(__file__).parent, INPUTS, EVIDENCE_CLASS, (("a", case_a), ("b", case_b), ("c", case_c)))
    print(passed)
    raise SystemExit(0 if passed and all(passed.values()) else 1)
