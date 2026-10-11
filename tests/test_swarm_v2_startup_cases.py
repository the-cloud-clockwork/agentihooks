import json
from pathlib import Path

import pytest

from tests import sv2_prf02_cases as cases

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-PRF-02"


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = cases.run_case(case), cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text()) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True)


def test_committed_results_page_matches_the_recorded_reads():
    page = Path(__file__).parents[1] / "docs" / "swarm-v2" / "startup-results.md"
    assert cases.results_page() == page.read_text()


def test_every_recorded_read_names_its_source_and_hashes_its_listings():
    for name in cases.INPUTS:
        read = cases.load(name)
        assert cases.SOURCES[name]
        assert sorted(read["inputs"]) == ["activities", "events", "instances", "nodes", "pods"]
