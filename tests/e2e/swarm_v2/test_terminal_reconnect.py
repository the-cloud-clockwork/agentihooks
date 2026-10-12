import json
from pathlib import Path

import pytest

from tests import sv2_hdr05_cases

EVIDENCE = Path(__file__).parents[3] / "evidence" / "SV2-HDR-05"


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = sv2_hdr05_cases.run_case(case), sv2_hdr05_cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text()) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True)


def test_the_manifest_and_package_result_name_the_case_inputs_and_records():
    manifest = json.loads((EVIDENCE / "manifest.json").read_text())
    result = json.loads((EVIDENCE / "result.json").read_text())
    for name, digest in sv2_hdr05_cases.run_case("a")["input_sha256"].items():
        assert manifest["inputs"][f"tests/fixtures/swarm_v2/{name}"] == digest
    assert manifest["cases"] == ["T-SV2-HDR-05-A", "T-SV2-HDR-05-B", "T-SV2-HDR-05-C"]
    assert result["package"] == "SV2-HDR-05"
    assert result["tested_commit"] == manifest["tested_commit"]
    assert result["package_complete"] is False
    assert set(result["states"]) == {"observed", "accepted", "committed", "externally_verified"}
    for case in "abc":
        assert (EVIDENCE.parents[1] / result["cases"][case]).exists()
