from pathlib import Path

from scripts.swarm_v2.case_results import write
from tests.contracts.storage_layout.cases import case_a, case_b, case_c

ROOT = Path(__file__).resolve().parents[2]
INPUTS = (
    "scripts/swarm_v2/kubernetes/storage.py",
    "scripts/swarm_v2/kubernetes/spec.py",
    "scripts/swarm_v2/cache.py",
    "scripts/swarm_v2/artifacts/publication.py",
    "docs/swarm-v2/schemas/pod-policy.json",
    "tests/contracts/storage_layout/test_pod_storage.py",
    "tests/contracts/storage_layout/test_publication.py",
    "tests/contracts/storage_layout/cases.py",
    "tests/test_swarm_v2_cache.py",
    "evidence/SV2-FSY-05/generate_case_results.py",
    "Swarm-v2.md",
)
EVIDENCE_CLASS = (
    "mocked: rendered Pod manifests checked without a cluster, a cache store whose read only mount is simulated "
    "by the store's mount probe, and a local artifact directory replaced by a file to model lost shared storage; "
    "no NFS, no Pod and no live rollout proof"
)

if __name__ == "__main__":
    passed = write(ROOT, Path(__file__).parent, INPUTS, EVIDENCE_CLASS, (("a", case_a), ("b", case_b), ("c", case_c)))
    print(passed)
    raise SystemExit(0 if passed and all(passed.values()) else 1)
