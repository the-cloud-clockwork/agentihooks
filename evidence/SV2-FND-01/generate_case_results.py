import hashlib
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

CASES = {
    "a": (
        "T-SV2-FND-01-A",
        (
            "test_report_separates",
            "test_second_independent",
            "test_interfaces_are_present",
            "test_markdown_",
            "test_unique_revision",
            "test_http_probe[",
            "test_http_probe_uses",
            "test_command_output",
            "test_utc_now",
            "test_repo_url",
            "test_subprocess_calls",
            "test_minimal_repository",
            "test_cli_requires",
            "test_unverified_count",
        ),
    ),
    "b": (
        "T-SV2-FND-01-B",
        (
            "test_unavailable_endpoint",
            "test_missing_command",
            "test_branch_absent",
            "test_git_and_command_timeouts",
            "test_failing_or_empty",
            "test_unsupported_probe",
            "test_http_probe_without",
            "test_read_only",
            "test_load_sources_refuses",
            "test_sanitize",
            "test_interfaces_without",
            "test_missing_git",
            "test_git_that_does_not",
            "test_ref_line",
        ),
    ),
    "c": (
        "T-SV2-FND-01-C",
        (
            "test_rerun_after",
            "test_unresolved_rerun",
            "test_cli_writes",
            "test_interrupted_write",
            "test_older_previous",
            "test_missing_previous",
            "test_failed_write",
        ),
    ),
}
INPUTS = (
    "tests/fixtures/swarm_v2/baseline/sources.json",
    "tests/test_swarm_v2_baseline.py",
    "scripts/swarm_v2/baseline.py",
    "docs/swarm-v2/baseline-sources.json",
    "docs/swarm-v2/plan-snapshot.json",
)


def _outcome(case: ET.Element) -> str:
    if case.find("failure") is not None or case.find("error") is not None:
        return "failed"
    return "skipped" if case.find("skipped") is not None else "passed"


def _version(*argv: str) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=20).stdout.strip().splitlines()[0]
    except (OSError, IndexError, subprocess.TimeoutExpired) as exc:
        return f"unverified ({type(exc).__name__})"


def main(junit: str, commit: str, pull_request: str) -> None:
    cases = {key: [] for key in CASES}
    for case in ET.parse(junit).iter("testcase"):
        name = case.get("name")
        key = next(k for k, (_, prefixes) in CASES.items() if name.startswith(prefixes))
        cases[key].append({"test": f"tests/test_swarm_v2_baseline.py::{name}", "outcome": _outcome(case)})
    out = Path("evidence/SV2-FND-01")
    manifest = {
        "package": "SV2-FND-01",
        "tested_commit": commit,
        "pull_request": pull_request,
        "inputs": {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in INPUTS},
        "versions": {
            "python": sys.version.split()[0],
            "git": _version("git", "--version"),
            "kubectl": _version("kubectl", "version", "--client"),
        },
        "generated_by": "pytest tests/test_swarm_v2_baseline.py -p no:xdist -p no:randomly --junitxml=<file>, "
        "then python evidence/SV2-FND-01/generate_case_results.py <file> <commit> <pull request>",
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
