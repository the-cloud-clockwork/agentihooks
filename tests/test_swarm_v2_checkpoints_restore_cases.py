import json
from pathlib import Path

import pytest

from tests import sv2_ckp05_cases as cases

pytestmark = pytest.mark.unit
EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-CKP-05"


@pytest.mark.parametrize("case", ["A", "B", "C"])
def test_package_cases_match_committed_evidence(tmp_path, case):
    observed = cases.run_case(case, tmp_path)
    path = EVIDENCE / f"{case.lower()}-result.json"
    expected = json.loads(path.read_text()) if path.exists() else None
    assert observed == expected, json.dumps(observed, sort_keys=True)


def test_package_record_names_limitations_and_fixture_inputs():
    record = json.loads((EVIDENCE / "result.json").read_text())
    manifest = json.loads((EVIDENCE / "manifest.json").read_text())
    case = json.loads((EVIDENCE / "a-result.json").read_text())
    assert record["remaining_limitations"]
    assert record["fixture_inputs"] == cases.FIXTURE_INPUTS == manifest["fixture_inputs"]
    assert manifest["input_sha256"] == case["input_sha256"]
    assert record["mocked"] is manifest["mocked"] is True
