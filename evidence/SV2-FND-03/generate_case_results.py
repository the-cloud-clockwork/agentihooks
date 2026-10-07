import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

CASES = {
    "a": (
        "T-SV2-FND-03-A",
        (
            "test_a_consumer_validates",
            "test_a_second_independent",
            "test_every_family",
            "test_committed_fixtures",
            "test_the_generator",
            "test_manifest_",
            "test_schemas_reference",
            "test_authority_names_are_reserved",
            "test_supported_families",
            "test_admitting_current",
            "test_writers_use",
            "test_load_reads",
            "test_the_admission_module_imports",
            "test_the_stored_record",
        ),
    ),
    "b": (
        "T-SV2-FND-03-B",
        (
            "test_a_consumer_fails",
            "test_a_future_major",
            "test_an_absent_execution",
            "test_a_record_without_authority",
            "test_the_counter",
            "test_an_unreadable",
            "test_a_minor_below",
            "test_a_non_object",
            "test_a_malformed",
            "test_an_unknown_authority",
            "test_an_authority_name_outside",
            "test_a_schema_failure",
            "test_a_missing_body",
            "test_every_refusal",
            "test_a_display_label",
            "test_another_execution",
        ),
    ),
    "c": (
        "T-SV2-FND-03-C",
        (
            "test_a_newer_minor",
            "test_a_replay",
            "test_a_reused",
            "test_an_older_generation",
            "test_the_same_generation",
            "test_an_interrupted",
            "test_admission_waits",
            "test_a_newer_generation_fences",
        ),
    ),
}
INPUTS = (
    "docs/swarm-v2/schemas/compatibility.json",
    "docs/swarm-v2/schemas/common.json",
    "tests/contracts/swarm_v2/fixtures/index.json",
    "tests/contracts/swarm_v2/consumer.py",
    "tests/contracts/swarm_v2/build_fixtures.py",
    "tests/contracts/swarm_v2/test_consumer.py",
    "tests/contracts/swarm_v2/test_admission.py",
    "scripts/swarm_v2/contracts.py",
)


def _outcome(case: ET.Element) -> str:
    if case.find("failure") is not None or case.find("error") is not None:
        return "failed"
    return "skipped" if case.find("skipped") is not None else "passed"


def main(junit: str, commit: str, pull_request: str) -> None:
    cases = {key: [] for key in CASES}
    for case in ET.parse(junit).iter("testcase"):
        name = case.get("name")
        module = case.get("classname").rsplit(".", 1)[-1]
        key = next(k for k, (_, prefixes) in CASES.items() if name.startswith(prefixes))
        cases[key].append({"test": f"tests/contracts/swarm_v2/{module}.py::{name}", "outcome": _outcome(case)})
    out = Path("evidence/SV2-FND-03")
    manifest = {
        "package": "SV2-FND-03",
        "tested_commit": commit,
        "pull_request": pull_request,
        "inputs": {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in INPUTS},
        "versions": {"python": sys.version.split()[0]},
        "generated_by": "pytest tests/contracts/swarm_v2 -p no:xdist -p no:randomly --junitxml=<file>, "
        "then python evidence/SV2-FND-03/generate_case_results.py <file> <commit> <pull request>",
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
