import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

CASES = {
    "a": (
        "T-SV2-FND-02-A",
        (
            "test_committed_",
            "test_fixture_review",
            "test_recording_the_fixture",
            "test_second_independent",
            "test_render_",
            "test_cli_review",
            "test_cli_record",
            "test_cli_check_passes",
            "test_review_accepts",
            "test_main_defaults",
        ),
    ),
    "b": (
        "T-SV2-FND-02-B",
        (
            "test_review_rejects",
            "test_an_unlisted",
            "test_unowned_proposals",
            "test_an_unowned_recorded",
            "test_check_reports",
            "test_check_lists",
            "test_rejection_reasons",
            "test_a_corrected",
            "test_an_undeclared",
            "test_the_declaration",
            "test_cli_check_fails",
            "test_an_operator_change",
            "test_a_proposal_cannot",
            "test_a_stale_revision",
            "test_a_newer_base",
            "test_documents_with",
            "test_an_inventory_with",
            "test_cli_reports",
        ),
    ),
    "c": (
        "T-SV2-FND-02-C",
        (
            "test_conflicting_proposals",
            "test_a_replayed",
            "test_a_replay_with",
            "test_digest_",
            "test_an_interrupted_write",
            "test_a_written_record",
            "test_rollback_",
            "test_cli_rollback",
            "test_cli_render",
        ),
    ),
}
INPUTS = (
    "tests/fixtures/swarm_v2/architecture/inventory.json",
    "tests/fixtures/swarm_v2/architecture/inventory-second.json",
    "tests/test_swarm_v2_architecture.py",
    "scripts/swarm_v2/architecture.py",
    "docs/swarm-v2/architecture.json",
    "docs/swarm-v2/decisions.md",
)


def _outcome(case: ET.Element) -> str:
    if case.find("failure") is not None or case.find("error") is not None:
        return "failed"
    return "skipped" if case.find("skipped") is not None else "passed"


def main(junit: str, commit: str, pull_request: str) -> None:
    cases = {key: [] for key in CASES}
    for case in ET.parse(junit).iter("testcase"):
        name = case.get("name")
        key = next(k for k, (_, prefixes) in CASES.items() if name.startswith(prefixes))
        cases[key].append({"test": f"tests/test_swarm_v2_architecture.py::{name}", "outcome": _outcome(case)})
    out = Path("evidence/SV2-FND-02")
    manifest = {
        "package": "SV2-FND-02",
        "tested_commit": commit,
        "pull_request": pull_request,
        "inputs": {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in INPUTS},
        "versions": {"python": sys.version.split()[0]},
        "generated_by": "pytest tests/test_swarm_v2_architecture.py -p no:xdist -p no:randomly --junitxml=<file>, "
        "then python evidence/SV2-FND-02/generate_case_results.py <file> <commit> <pull request>",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    for key, (case_id, _) in CASES.items():
        tests = cases[key]
        passed = sum(t["outcome"] == "passed" for t in tests)
        result = {"case": case_id, "tested_commit": commit, "passed": passed, "failed": len(tests) - passed}
        (out / f"{key}-result.json").write_text(json.dumps({**result, "tests": tests}, indent=2) + "\n")
        print(case_id, passed, len(tests) - passed)


if __name__ == "__main__":
    main(*sys.argv[1:4])
