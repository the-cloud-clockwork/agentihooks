"""The tick registry refuses a step without a feature map entry and a scenario when given a catalogue."""

import json
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class FakeStep:
    name: str
    feature: str = ""
    needs: frozenset[str] = frozenset()
    writes: frozenset[str] = frozenset()
    reads: frozenset[str] = frozenset()
    effects: frozenset[str] = frozenset()
    lane: str = "local"
    group: str = "observe"
    budget_s: float = 1.0
    interval_s: float = 0.0

    def run(self, ctx: object) -> object:
        return ctx


def registry() -> Any:
    return import_module("scripts.tick.registry")


def catalogue() -> Any:
    return import_module("scripts.tick.catalogue")


def subject(*steps: FakeStep) -> Any:
    found = registry().Registry()
    for step in steps:
        found.register(step)
    return found


def mapped(unmapped: frozenset[str] = frozenset(), **scenarios: frozenset[str]) -> Any:
    return registry().Catalogue(frozenset({"tick-feature"}), scenarios, unmapped)


def errors(found: Any, given: Any) -> str:
    with pytest.raises(ValueError) as error:
        found.validate(given)
    return str(error.value)


def test_production_validates_without_a_catalogue() -> None:
    assert subject(FakeStep("bare")).validate() is None


def test_a_step_with_no_feature_is_refused() -> None:
    found = subject(FakeStep("bare"))
    assert errors(found, mapped(bare=frozenset({"running"}))) == "Step bare: feature '' is not a feature map id"


def test_a_step_with_an_unknown_feature_is_refused() -> None:
    found = subject(FakeStep("odd", feature="other"))
    assert errors(found, mapped(odd=frozenset({"running"}))) == "Step odd: feature 'other' is not a feature map id"


def test_a_step_with_a_feature_and_no_scenario_is_refused() -> None:
    found = subject(FakeStep("lonely", feature="tick-feature"))
    assert errors(found, mapped()) == "Step lonely: no scenario lists it"
    assert errors(found, mapped(lonely=frozenset())) == "Step lonely: no scenario lists it"


def test_a_step_missing_both_names_both() -> None:
    found = subject(FakeStep("bare"))
    assert errors(found, mapped()) == "Step bare: feature '' is not a feature map id; no scenario lists it"


def test_a_mapped_step_and_an_unmapped_one_pass() -> None:
    found = subject(FakeStep("full", feature="tick-feature"), FakeStep("bare"))
    assert found.validate(mapped(frozenset({"bare"}), full=frozenset({"running"}))) is None


def test_an_unmapped_entry_for_a_mapped_step_is_refused() -> None:
    found = subject(FakeStep("full", feature="tick-feature"))
    given = mapped(frozenset({"full"}), full=frozenset({"running"}))
    assert errors(found, given) == "Step full is mapped; drop it from the unmapped steps"


def test_an_unmapped_entry_for_no_registered_step_is_refused() -> None:
    assert errors(subject(), mapped(frozenset({"gone"}))) == "Unmapped step gone is not registered; drop it"


def test_every_refusal_is_reported() -> None:
    found = subject(FakeStep("a"), FakeStep("b", feature="tick-feature"))
    assert errors(found, mapped()) == (
        "Step a: feature '' is not a feature map id; no scenario lists it\nStep b: no scenario lists it"
    )


def test_the_module_validate_passes_the_catalogue(monkeypatch: pytest.MonkeyPatch) -> None:
    module = registry()
    monkeypatch.setattr(module, "registry", subject(FakeStep("bare")))
    assert module.validate() is None
    with pytest.raises(ValueError, match="Step bare: feature '' is not a feature map id"):
        module.validate(mapped(bare=frozenset({"running"})))


def tree(tmp_path: Path, index: dict[str, Any]) -> Path:
    root = tmp_path / "tree"
    (root / "tests/swarm/tick_scenarios").mkdir(parents=True)
    (root / "feature-map.json").write_text(json.dumps({"features": [{"id": "tick-feature"}], "unmapped_rules": []}))
    goldens = {"run_tick/running": {}, "tick/running": {}, "tick/paused": {}}
    (root / "tests/swarm/tick_scenarios/goldens.json").write_text(json.dumps(goldens))
    (root / "tests/swarm/tick_scenarios/steps.json").write_text(json.dumps(index))
    return root


def test_load_reads_the_feature_map_and_the_index(tmp_path: Path) -> None:
    root = tree(tmp_path, {"scenarios": {"full": ["running", "paused"]}, "unmapped": ["bare"]})
    assert catalogue().load(root) == registry().Catalogue(
        frozenset({"tick-feature"}), {"full": frozenset({"running", "paused"})}, frozenset({"bare"})
    )


def test_load_refuses_an_unknown_scenario(tmp_path: Path) -> None:
    root = tree(tmp_path, {"scenarios": {"full": ["running", "missing"]}, "unmapped": []})
    with pytest.raises(ValueError, match="names unknown scenarios: missing"):
        catalogue().load(root)


def test_a_planted_index_drop_is_red(tmp_path: Path) -> None:
    found = subject(FakeStep("full", feature="tick-feature"))
    kept = tree(tmp_path, {"scenarios": {"full": ["running"]}, "unmapped": []})
    assert found.validate(catalogue().load(kept)) is None
    (kept / "tests/swarm/tick_scenarios/steps.json").write_text(json.dumps({"scenarios": {}, "unmapped": []}))
    assert errors(found, catalogue().load(kept)) == "Step full: no scenario lists it"


def test_the_unmapped_steps_only_shrink() -> None:
    module = catalogue()
    assert module.grown(None, frozenset({"a"})) == []
    assert module.grown(frozenset({"a", "b"}), frozenset({"a"})) == []
    assert module.grown(frozenset({"a"}), frozenset({"a", "c", "b"})) == [
        "Unmapped step b is new; map it to a feature and a scenario instead",
        "Unmapped step c is new; map it to a feature and a scenario instead",
    ]


def test_unmapped_reads_a_tree_or_nothing(tmp_path: Path) -> None:
    module = catalogue()
    assert module.unmapped(tmp_path) is None
    root = tree(tmp_path, {"scenarios": {}, "unmapped": ["b", "a"]})
    assert module.unmapped(root) == frozenset({"a", "b"})


def test_the_recorded_catalogue_holds_every_registered_step() -> None:
    assert catalogue().check(ROOT) == []


def test_check_reports_a_step_the_index_drops(tmp_path: Path) -> None:
    root = tree(tmp_path, {"scenarios": {}, "unmapped": []})
    found = catalogue().check(root)
    assert found
    assert all(line.endswith("feature '' is not a feature map id; no scenario lists it") for line in found)


def test_shrink_refuses_a_grown_unmapped_list(tmp_path: Path) -> None:
    base = tree(tmp_path / "base", {"scenarios": {}, "unmapped": ["a"]})
    head = tree(tmp_path / "head", {"scenarios": {}, "unmapped": ["b", "a"]})
    assert catalogue().shrink(base, head) == ["Unmapped step b is new; map it to a feature and a scenario instead"]
    assert catalogue().shrink(head, base) == []
    assert catalogue().shrink(tmp_path / "none", head) == []


def test_main_prints_each_error_and_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = tree(tmp_path / "base", {"scenarios": {}, "unmapped": []})
    head = tree(tmp_path / "head", {"scenarios": {}, "unmapped": ["a"]})
    assert catalogue().main(["shrink", "--base", str(base), "--head", str(head)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "1 tick catalogue errors",
        "::error::Unmapped step a is new; map it to a feature and a scenario instead",
    ]
    assert catalogue().main(["shrink", "--base", str(head), "--head", str(base)]) == 0
    assert catalogue().main(["check", "--root", str(ROOT)]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "0 tick catalogue errors"
