import hashlib
import json
import re
import sys
from pathlib import Path

CASES = {"a": "T-SV2-VAL-01-A", "b": "T-SV2-VAL-01-B", "c": "T-SV2-VAL-01-C"}
INPUTS = (
    "docs/swarm-v2/schemas/compatibility.json",
    "docs/swarm-v2/schemas/common.json",
    "docs/swarm-v2/schemas/error.json",
    "tests/contracts/swarm_v2/fixtures/index.json",
    "tests/contracts/swarm_v2/fixtures/matrix.json",
    "tests/contracts/swarm_v2/conformance.py",
    "tests/contracts/swarm_v2/agentihooks_target.py",
    "tests/contracts/swarm_v2/test_conformance.py",
    "scripts/swarm_v2/contracts.py",
)
LINE = re.compile(r"(PASSED|FAILED|ERROR|SKIPPED) (tests/contracts/swarm_v2/test_conformance\.py::test_([abc])_\S+)")


def main(log: str, report: str, commit: str, run: str) -> None:
    out = Path("evidence/SV2-VAL-01")
    tests = {key: {} for key in CASES}
    for match in LINE.finditer(Path(log).read_text()):
        tests[match[3]][match[2]] = match[1].lower()
    conformance = json.loads(Path(report).read_text())
    (out / "compatibility-results.json").write_text(json.dumps(conformance, indent=2) + "\n")
    manifest = {
        "package": "SV2-VAL-01",
        "tested_commit": commit,
        "ci_run": run,
        "inputs": {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in INPUTS},
        "mocked": True,
        "generated_by": "Proofs workflow tests proof on tests/contracts/swarm_v2, then python "
        "evidence/SV2-VAL-01/generate_case_results.py <run log> <proof report> <commit> <run url>",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    for key, case in CASES.items():
        found = [{"test": name, "outcome": outcome} for name, outcome in sorted(tests[key].items())]
        passed = sum(t["outcome"] == "passed" for t in found)
        result = {
            "case": case,
            "tested_commit": commit,
            "ci_run": run,
            "contract_cases_passed": conformance["contract_cases_passed"],
            "contract_cases_failed": conformance["contract_cases_failed"],
            "passed": passed,
            "failed": len(found) - passed,
            "tests": found,
        }
        (out / f"{key}-result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(case, passed, len(found) - passed)


if __name__ == "__main__":
    main(*sys.argv[1:5])
