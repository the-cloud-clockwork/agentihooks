import hashlib
import json
from pathlib import Path

import pytest

from scripts.swarm_v2 import startup_stages as stages
from tests import sv2_prf02_cases as cases

pytestmark = pytest.mark.unit

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


def test_the_committed_worker_logs_reproduce_the_native_read():
    read = cases.load(cases.NATIVE)
    folder = cases.FIXTURES / "logs"
    for name, digest in read["inputs"]["logs"].items():
        text = (folder / name).read_bytes()
        assert hashlib.sha256(text).hexdigest() == digest
        pod = Path(name).stem.replace("_", "/", 1)
        marks = stages.log_marks(text.decode())
        assert {key: read["pods"][pod][key] for key in ("prepared", "delivered")} == marks


def test_every_recorded_read_names_its_source_and_hashes_its_listings():
    for name in cases.INPUTS:
        read = cases.load(name)
        assert cases.SOURCES[name]
        logged = ["logs"] if name == cases.NATIVE else []
        assert sorted(read["inputs"]) == sorted(["activities", "events", "instances", "nodes", "pods", *logged])
