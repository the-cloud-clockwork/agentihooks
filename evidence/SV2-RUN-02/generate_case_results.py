import sys
from pathlib import Path

from scripts.swarm_v2 import case_results
from tests.sv2_run02_cases import case_a, case_b, case_c

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_CLASS = (
    "local isolated fixture on temporary homes; mocked: the herdr CLI and the process reaper behind HerdrRuntime, "
    "the local process table and PID namespace are fixture values, and the remote backend is an in-memory fake; "
    "not live rollout proof"
)
INPUTS = (
    "scripts/swarm_v2/runtime/base.py",
    "scripts/swarm_v2/runtime/local.py",
    "scripts/swarm_v2/runtime/process.py",
    "scripts/swarm_v2/runtime/routed.py",
    "scripts/swarm/runtime.py",
    "scripts/swarm/tick.py",
    "scripts/swarm/execution.py",
    "scripts/swarm/reaper.py",
    "tests/test_swarm_v2_process.py",
    "tests/test_swarm_v2_runtime.py",
    "tests/swarm/test_tick.py",
    "tests/sv2_run02_cases.py",
    "scripts/swarm_v2/case_results.py",
    "evidence/SV2-RUN-02/generate_case_results.py",
)


if __name__ == "__main__":
    passed = case_results.write(
        ROOT, ROOT / "evidence/SV2-RUN-02", INPUTS, EVIDENCE_CLASS, (("a", case_a), ("b", case_b), ("c", case_c))
    )
    print(case_results.UNCOMMITTED if passed is None else passed)
    sys.exit(2 if passed is None else int(not all(passed.values())))
