import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from tests.contracts.storage_layout import cases

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
EVIDENCE = ROOT / "evidence" / "SV2-FSY-05"
PLAN = "Swarm-v2.md"
STAMP = ("tested_commit", "evidence_class")


def _generator():
    spec = importlib.util.spec_from_file_location("sv2_fsy05_generator", EVIDENCE / "generate_case_results.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _digest(path: str) -> str:
    return hashlib.sha256((ROOT / path).read_bytes()).hexdigest()


def _committed(name: str) -> dict | None:
    path = EVIDENCE / name
    return json.loads(path.read_text()) if path.exists() else None


@pytest.mark.parametrize("name", ["a", "b", "c"])
def test_each_committed_case_result_matches_a_fresh_run_of_its_case(name):
    fresh = getattr(cases, f"case_{name}")()
    assert fresh["passed"] is True, json.dumps(fresh, indent=2)
    committed = _committed(f"{name}-result.json")
    stamp = {key: (committed or {}).get(key) for key in STAMP}
    assert committed == {**stamp, **fresh}, json.dumps(fresh, indent=2)


def test_the_manifest_hashes_every_current_input_of_the_record():
    inputs = _generator().INPUTS
    current = {path: _digest(path) for path in inputs if path != PLAN}
    manifest = _committed("manifest.json") or {"inputs": {}}
    assert sorted(manifest["inputs"]) == sorted(inputs), json.dumps(current, indent=2)
    recorded = {path: digest for path, digest in manifest["inputs"].items() if path != PLAN}
    assert recorded == current, json.dumps(current, indent=2)


def test_the_case_results_and_manifest_name_one_tested_commit_and_the_generator_class():
    generator = _generator()
    manifest = _committed("manifest.json") or {}
    stamps = [{key: (_committed(f"{name}-result.json") or {}).get(key) for key in STAMP} for name in "abc"]
    expected = {"tested_commit": manifest.get("tested_commit"), "evidence_class": generator.EVIDENCE_CLASS}
    assert len(expected["tested_commit"] or "") == 40
    assert stamps == [expected] * 3
