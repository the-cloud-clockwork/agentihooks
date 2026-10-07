import argparse
import hashlib
import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

CASES = {
    "a": (
        "canonical_checkout",
        "swarm_does_not_confuse",
        "explicit_non_git",
        "git_remote_cannot",
        "normalized_forge",
        "metadata_schema",
    ),
    "b": ("unsafe_or_ambiguous", "invalid_non_git", "invalid_alias_maps", "registration_cannot", "alias_map_checks"),
    "c": ("alias_rename", "alias_chain"),
}


def generate(junit: Path) -> dict:
    root = Path(__file__).resolve().parents[2]
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    report = ET.parse(junit).getroot()
    tests = [
        row for row in report.iter("testcase") if row.get("classname", "").endswith("test_canonical_project_identity")
    ]
    if not tests or any(row.find(tag) is not None for row in tests for tag in ("failure", "error", "skipped")):
        raise ValueError("Identity test report is absent or unsuccessful")
    output = root / "evidence/SV2-IDN-01"
    results = {}
    for case, names in CASES.items():
        selected = [row for row in tests if any(row.get("name", "").startswith(f"test_{name}") for name in names)]
        if not selected:
            raise ValueError("Identity acceptance case is absent")
        results[case] = {
            "case": f"T-SV2-IDN-01-{case.upper()}",
            "tested_commit": commit,
            "evidence_class": "local isolated Git fixtures; not live rollout or CI proof",
            "passed": len(selected),
            "tests": [row.get("name") for row in selected],
            "project_identity_ambiguities_total": sum(
                int(prop.get("value", "0"))
                for row in selected
                for prop in row.findall("properties/property")
                if prop.get("name") == "project_identity_ambiguities_total"
            ),
        }
    manifest = {
        "tested_commit": commit,
        "inputs": {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (
                root / "tests/fixtures/swarm_v2/project-identity.json",
                root / "hooks/context/project_identity.py",
                root / "docs/swarm-v2/schemas/project.json",
                root / "tests/context/test_canonical_project_identity.py",
            )
        },
        "junit_sha256": hashlib.sha256(junit.read_bytes()).hexdigest(),
    }
    for case, result in results.items():
        (output / f"{case}-result.json").write_text(json.dumps(result, indent=2) + "\n")
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return {"tested_commit": commit, "cases": {case: row["passed"] for case, row in results.items()}}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--junit", type=Path, required=True)
    print(json.dumps(generate(parser.parse_args().junit)))


if __name__ == "__main__":
    main()
