import ast
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

PACKAGE = "SV2-FND-04"
TESTS = "tests/test_swarm_v2_evidence.py"
MARKER = re.compile(r"^# (T-SV2-FND-04-([ABC]))")
INPUTS = (
    "Swarm-v2.md",
    "docs/swarm-v2/evidence-index.json",
    "docs/swarm-v2/evidence-index.md",
    "tests/fixtures/swarm_v2/evidence/packages.json",
    "tests/fixtures/swarm_v2/evidence/packages-second.json",
    "tests/fixtures/swarm_v2/evidence/healthy-pod.png",
    TESTS,
    "scripts/swarm_v2/validate_plan.py",
    "scripts/ci_mutation/runner.py",
)


def _sections() -> list[tuple[int, str, str]]:
    lines = Path(TESTS).read_text().splitlines()
    return [(n, m.group(1), m.group(2).lower()) for n, line in enumerate(lines) if (m := MARKER.match(line))]


def _outcome(case: ET.Element) -> str:
    if case.find("failure") is not None or case.find("error") is not None:
        return "failed"
    return "skipped" if case.find("skipped") is not None else "passed"


def main(junit: str, commit: str, pull_request: str) -> None:
    sections = _sections()
    tree = ast.parse(Path(TESTS).read_text())
    defined = {node.name: node.lineno - 1 for node in tree.body if isinstance(node, ast.FunctionDef)}
    cases = {key: (case_id, []) for _, case_id, key in sections}
    for case in ET.parse(junit).iter("testcase"):
        if not case.get("classname", "").endswith("test_swarm_v2_evidence"):
            continue
        line = defined[case.get("name").split("[")[0]]
        _, _, key = [s for s in sections if s[0] < line][-1]
        cases[key][1].append({"test": f"{TESTS}::{case.get('name')}", "outcome": _outcome(case)})
    out = Path("evidence") / PACKAGE
    manifest = {
        "package": PACKAGE,
        "tested_commit": commit,
        "pull_request": pull_request,
        "inputs": {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in INPUTS},
        "versions": {"python": sys.version.split()[0]},
        "generated_by": f"pytest {TESTS} -p no:xdist -p no:randomly --junitxml=<file>, "
        f"then python evidence/{PACKAGE}/generate_case_results.py <file> <commit> <pull request>",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    for key, (case_id, tests) in sorted(cases.items()):
        passed = sum(t["outcome"] == "passed" for t in tests)
        result = {"case": case_id, "tested_commit": commit, "passed": passed, "failed": len(tests) - passed}
        (out / f"{key}-result.json").write_text(json.dumps({**result, "tests": tests}, indent=2) + "\n")
        print(case_id, passed, len(tests) - passed)


if __name__ == "__main__":
    main(*sys.argv[1:4])
