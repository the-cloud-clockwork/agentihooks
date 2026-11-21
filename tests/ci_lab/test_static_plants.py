import json
from pathlib import Path
from typing import Any

import pytest

from scripts import ci_wiring
from scripts.ci_lab import executors, expressions
from scripts.ci_lab.static_gates import REGISTRY, STATIC_JOBS, WORKFLOW, StaticGate, discover

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
PLANTS = ROOT / "tests" / "ci_lab" / "plants"
EXCLUSIONS = ROOT / "tests" / "ci_lab" / "static_gate_exclusions.json"
NEEDS = {"step env", "secret", "checkout", "earlier step", "docker", "network"}
UNSUPPLIED = {"step env", "secret"}
CONTEXT = {executors.INPUTS_BASE: "0" * 40}


def gates() -> dict[str, StaticGate]:
    registry = json.loads((ROOT / REGISTRY).read_text())
    workflow = (ROOT / WORKFLOW).read_text()
    return {gate.name: gate for gate in discover(workflow, registry, ci_wiring.load(ROOT))}


def plants() -> dict[str, dict[str, Any]]:
    found = {path.stem: json.loads(path.read_text()) for path in sorted(PLANTS.glob("*.json"))}
    return {name: spec for name, spec in found.items() if spec["gate"] not in STATIC_JOBS}


def exclusions() -> dict[str, dict[str, Any]]:
    return json.loads(EXCLUSIONS.read_text())


def supplied(gate: StaticGate) -> bool:
    try:
        expressions.holds(gate.condition, CONTEXT)
        for _, value in gate.env:
            expressions.interpolate(value, CONTEXT)
    except ValueError:
        return False
    return True


def test_every_discovered_static_gate_has_one_plant_or_one_exclusion() -> None:
    planted = [spec["gate"] for spec in plants().values()]
    assert len(planted) == len(set(planted))
    assert not set(planted) & set(exclusions())
    assert sorted({*planted, *exclusions()}) == sorted(gates())


def test_a_gate_plant_is_named_after_its_gate() -> None:
    expected = {name: name.replace(".", "/") for name in plants()}
    expected["lint.threshold-pins"] = "lint/pin-thresholds-floors-and-allowlists-against-the-base"
    assert {name: spec["gate"] for name, spec in plants().items()} == expected


@pytest.mark.parametrize("name", sorted(plants()))
def test_a_gate_plant_edits_this_tree_once_per_edit(name: str) -> None:
    spec = plants()[name]
    assert spec["reason"]
    assert spec["edits"]
    for edit in spec["edits"]:
        assert (ROOT / edit["path"]).read_text().count(edit["before"]) == 1
        assert edit["after"] != edit["before"]


def test_a_planted_gate_reads_only_the_lab_context() -> None:
    found = gates()
    assert [spec["gate"] for spec in plants().values() if not supplied(found[spec["gate"]])] == []


def test_an_exclusion_names_what_the_lab_lacks() -> None:
    for entry in exclusions().values():
        assert entry["reason"]
        assert entry["needs"]
        assert set(entry["needs"]) <= NEEDS
    unsupplied = {name for name, gate in gates().items() if not supplied(gate)}
    assert unsupplied == {name for name, entry in exclusions().items() if UNSUPPLIED & set(entry["needs"])}
