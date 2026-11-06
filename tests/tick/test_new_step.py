"""The step scaffold writes a step and its four artefacts so the tick pattern, catalogue and feature map gates pass."""

import ast
import json
import shutil
from importlib import import_module
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
COPIED = (
    "scripts/tick/steps/__init__.py",
    "scripts/tick/steps/budgets.py",
    "tests/swarm/tick_scenarios/steps.json",
    "tests/swarm/tick_scenarios/goldens.json",
)
STEP = "probe widgets"
OTHER = "count widgets"
ARGS = ["--group", "widgets", "--lane", "tick", "--interval", "120", "--effects", "ledger,inbox"]


def new_step() -> Any:
    """The scaffold module."""
    return import_module("scripts.tick.new_step")


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A temporary tree holding the files the scaffold edits and an empty feature map."""
    for name in COPIED:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, tmp_path / name)
    (tmp_path / "feature-map.json").write_text(json.dumps({"features": []}, indent=2) + "\n")
    return tmp_path


def scaffold(tree: Path, name: str, feature: str = "tick-step-widgets") -> int:
    """Run the scaffold command in ``tree`` for step ``name`` in the widgets module."""
    argv = [name, *ARGS, "--feature", feature, "--module", "widgets", "--root", str(tree)]
    return new_step().main(argv)


def load(path: Path, name: str) -> ModuleType:
    """Execute the source at ``path`` as a fresh module called ``name``."""
    spec = spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def registered(tree: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A fresh registry holding the steps of the scaffolded module, timed by the scaffolded budgets table."""
    registry = import_module("scripts.tick.registry")
    budgets = import_module("scripts.tick.steps.budgets")
    fresh = registry.Registry()
    monkeypatch.setattr(registry, "registry", fresh)
    monkeypatch.setattr(
        budgets, "MEASURED", load(tree / "scripts/tick/steps/budgets.py", "scaffolded_budgets").MEASURED
    )
    load(tree / "scripts/tick/steps/widgets.py", "scaffolded_widgets")
    return fresh


def test_a_scaffolded_step_passes_the_pattern_catalogue_and_feature_map_gates(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert scaffold(tree, STEP) == 0
    pattern = import_module("scripts.tick_pattern")
    texts = {
        name: (tree / name).read_text() for name in ("scripts/tick/steps/budgets.py", "scripts/tick/steps/widgets.py")
    }
    assert pattern.measured(texts) == {}
    fresh = registered(tree, monkeypatch)
    (step,) = fresh.steps()
    assert (step.name, step.group, step.lane, step.feature) == (STEP, "widgets", "tick", "tick-step-widgets")
    assert (step.effects, step.interval_s) == (frozenset({"ledger", "inbox"}), 120.0)
    assert import_module("scripts.feature_map").validate(tree, tree / "feature-map.json") == 1


def test_a_scaffolded_step_passes_the_catalogue_check_beside_every_registered_step(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = import_module("scripts.tick.registry").steps()
    assert scaffold(tree, STEP) == 0
    fresh = registered(tree, monkeypatch)
    for each in real:
        fresh.register(each)
    mapped = json.loads((tree / "feature-map.json").read_text())
    mapped["features"] += json.loads((ROOT / "feature-map.json").read_text())["features"]
    (tree / "feature-map.json").write_text(json.dumps(mapped))
    assert import_module("scripts.tick.catalogue").check(tree) == []
    assert STEP in {each.name for each in fresh.steps()}
    (tree / "tests/swarm/tick_scenarios/steps.json").write_text(
        (ROOT / "tests/swarm/tick_scenarios/steps.json").read_text()
    )
    assert import_module("scripts.tick.catalogue").check(tree) == [f"Step {STEP}: no scenario lists it"]


def plant(tree: Path, artefact: str) -> None:
    """Remove one artefact of the scaffolded step from ``tree``."""
    copied = {
        "budget row": "scripts/tick/steps/budgets.py",
        "index row": "tests/swarm/tick_scenarios/steps.json",
    }
    if artefact in copied:
        (tree / copied[artefact]).write_text((ROOT / copied[artefact]).read_text())
    elif artefact == "step":
        (tree / "scripts/tick/steps/widgets.py").write_text('"""The widgets steps of the tick."""\n')
    elif artefact == "test":
        (tree / "tests/tick/steps/test_probe_widgets.py").unlink()
    else:
        (tree / "feature-map.json").write_text(json.dumps({"features": []}, indent=2) + "\n")


@pytest.mark.parametrize("artefact", ["step", "budget row", "test", "index row", "feature entry"])
def test_check_goes_red_on_each_planted_missing_artefact(
    tree: Path, artefact: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert scaffold(tree, STEP) == 0
    plant(tree, artefact)
    capsys.readouterr()
    assert new_step().main(["--check", STEP, "--root", str(tree)]) == 1
    found = json.loads(capsys.readouterr().out)
    assert [name for name, present in found.items() if not present] == [artefact]


def test_a_new_module_joins_the_modules_the_registry_loads(tree: Path) -> None:
    assert scaffold(tree, STEP) == 0
    init = ast.parse((tree / "scripts/tick/steps/__init__.py").read_text())
    (modules,) = [
        node.value
        for node in init.body
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "MODULES" for t in node.targets)
    ]
    names = ast.literal_eval(modules)
    assert names[-1] == "scripts.tick.steps.widgets"
    assert names.count("scripts.tick.steps.widgets") == 1


def test_the_registry_loads_every_listed_step_module() -> None:
    steps = import_module("scripts.tick.steps")
    catalogue = import_module("scripts.tick.catalogue")
    assert sorted(steps.MODULES) == [name for name in catalogue.step_modules() if not name.endswith(".budgets")]
    names = {step.name for step in import_module("scripts.tick.registry").steps()}
    assert {"inbox relay", "snapshot job", "spawn"} <= names


def test_a_second_step_appends_to_the_existing_module(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert scaffold(tree, STEP) == 0
    assert scaffold(tree, OTHER, "tick-step-widget-count") == 0
    fresh = registered(tree, monkeypatch)
    assert [step.name for step in fresh.steps()] == [STEP, OTHER]
    assert import_module("scripts.feature_map").validate(tree, tree / "feature-map.json") == 2


def test_a_step_feature_already_mapped_gains_the_new_test(tree: Path) -> None:
    assert scaffold(tree, STEP) == 0
    assert scaffold(tree, OTHER) == 0
    (entry,) = json.loads((tree / "feature-map.json").read_text())["features"]
    assert entry["tests"] == [
        "tests/tick/steps/test_probe_widgets.py::test_probe_widgets",
        "tests/tick/steps/test_count_widgets.py::test_count_widgets",
    ]


def test_the_scaffold_refuses_an_existing_name(tree: Path) -> None:
    assert scaffold(tree, STEP) == 0
    before = (tree / "scripts/tick/steps/budgets.py").read_text()
    with pytest.raises(SystemExit) as refused:
        scaffold(tree, STEP)
    assert str(refused.value) == "probe widgets already has step, budget row, test, index row, feature entry"
    assert (tree / "scripts/tick/steps/budgets.py").read_text() == before
    with pytest.raises(SystemExit, match="spawn already has"):
        scaffold(tree, "spawn")


def test_the_scaffold_writes_its_test_expectations_and_the_index_in_their_canonical_form(tree: Path) -> None:
    assert scaffold(tree, STEP) == 0
    written = ast.parse((tree / "tests/tick/steps/test_probe_widgets.py").read_text())
    (compare,) = [node for node in ast.walk(written) if isinstance(node, ast.Compare)]
    (expected,) = compare.comparators
    assert isinstance(expected, ast.Tuple)
    assert [ast.literal_eval(each) for each in expected.elts[:3]] == ["widgets", "tick", "tick-step-widgets"]
    index = (tree / "tests/swarm/tick_scenarios/steps.json").read_text()
    assert index == json.dumps(json.loads(index), indent=1) + "\n"
    assert json.loads(index)["scenarios"][STEP] == ["running"]


def test_the_scaffold_runs_in_the_current_tree_with_no_ports(
    tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tree)
    argv = [OTHER, "--group", "widgets", "--lane", "slow", "--interval", "600", "--feature", "f", "--module", "widgets"]
    assert new_step().main(argv) == 0
    assert capsys.readouterr().out.splitlines()[0] == "scripts/tick/steps/widgets.py"
    fresh = registered(tree, monkeypatch)
    (step,) = fresh.steps()
    assert (step.lane, step.effects, step.interval_s) == ("slow", frozenset(), 600.0)


def test_the_scaffold_needs_a_lane_and_an_interval(tree: Path) -> None:
    argv = [STEP, "--group", "g", "--feature", "f", "--module", "widgets", "--root", str(tree)]
    with pytest.raises(SystemExit):
        new_step().main([*argv, "--interval", "60"])
    with pytest.raises(SystemExit):
        new_step().main([*argv, "--lane", "tick"])
    assert not (tree / "scripts/tick/steps/widgets.py").exists()


def test_the_scaffold_refuses_an_unknown_port(tree: Path) -> None:
    argv = [STEP, "--group", "g", "--lane", "tick", "--interval", "60", "--effects", "ledger,disk"]
    with pytest.raises(SystemExit, match="unknown port disk"):
        new_step().main([*argv, "--feature", "f", "--module", "widgets", "--root", str(tree)])


def test_check_reports_each_artefact_and_a_planted_missing_one_goes_red(
    tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert scaffold(tree, STEP) == 0
    capsys.readouterr()
    assert new_step().main(["--check", STEP, "--root", str(tree)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "step": True,
        "budget row": True,
        "test": True,
        "index row": True,
        "feature entry": True,
    }
    (tree / "tests/tick/steps/test_probe_widgets.py").unlink()
    assert new_step().main(["--check", STEP, "--root", str(tree)]) == 1
    assert json.loads(capsys.readouterr().out)["test"] is False
    with pytest.raises(ValueError, match="missing behaviour test"):
        import_module("scripts.feature_map").validate(tree, tree / "feature-map.json")


def test_check_reports_nothing_for_an_unknown_step(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert new_step().main(["--check", STEP, "--root", str(tree)]) == 1
    assert set(json.loads(capsys.readouterr().out).values()) == {False}


def test_the_scaffold_refuses_a_module_name_outside_the_steps_package(tree: Path) -> None:
    argv = [STEP, "--group", "g", "--lane", "tick", "--interval", "60", "--feature", "f", "--root", str(tree)]
    with pytest.raises(SystemExit) as refused:
        new_step().main([*argv, "--module", "budgets"])
    assert str(refused.value) == "budgets is not a steps module name"


def test_the_scaffold_refuses_an_unknown_lane(tree: Path) -> None:
    argv = [STEP, "--group", "g", "--lane", "fast", "--interval", "60", "--feature", "f", "--module", "widgets"]
    with pytest.raises(SystemExit):
        new_step().main([*argv, "--root", str(tree)])
    assert not (tree / "scripts/tick/steps/widgets.py").exists()


def test_the_scaffolded_test_expects_the_step_ports(tree: Path) -> None:
    assert scaffold(tree, STEP) == 0
    written = ast.parse((tree / "tests/tick/steps/test_probe_widgets.py").read_text())
    (compare,) = [node for node in ast.walk(written) if isinstance(node, ast.Compare)]
    (expected,) = compare.comparators
    assert isinstance(expected, ast.Tuple)
    assert ast.unparse(expected.elts[3]) == "frozenset({'inbox', 'ledger'})"


def test_a_dotted_import_binds_its_first_name() -> None:
    assert new_step()._bound(ast.parse("import os.path\nimport a.b as c\n")) == {"os", "c"}


def test_missing_imports_go_after_the_last_import_or_open_an_empty_module() -> None:
    added = "".join(line + "\n" for _, line in new_step().IMPORTS)
    assert new_step()._with_imports("") == "\n" + added
    source = '"""Doc."""\nimport os\n\nX = 1\n'
    assert new_step()._with_imports(source) == '"""Doc."""\nimport os\n\n' + added + "\nX = 1\n"
