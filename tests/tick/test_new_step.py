"""The step scaffold writes a step and its four artefacts so the tick pattern, catalogue and feature map gates pass."""

import ast
import json
import shutil
import subprocess
import sys
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
    monkeypatch.setattr(budgets, "FLOORS", load(tree / "scripts/tick/steps/budgets.py", "scaffolded_budgets").FLOORS)
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
    assert step.call.__name__ == "_probe_widgets"
    assert list(load(tree / "scripts/tick/steps/budgets.py", "scaffolded_rows").FLOORS)[-1] == STEP
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


def test_scaffold_writes_its_feature_fragment_without_touching_the_shared_map(tree: Path) -> None:
    map_path = tree / "feature-map.json"
    text = json.dumps({"features": [], "fragments": True})
    map_path.write_text(text)
    assert scaffold(tree, STEP) == 0
    assert map_path.read_text() == text
    entries = import_module("scripts.feature_map").load(map_path)["features"]
    assert len(entries) == 1
    assert entries[0]["tests"] == ["tests/tick/steps/test_probe_widgets.py::test_probe_widgets"]
    fragment = import_module("scripts.feature_map").entry_path(map_path, entries[0]["id"])
    assert fragment.read_text() == json.dumps(entries[0], indent=2) + "\n"
    assert import_module("scripts.tick.new_step").check(tree, STEP)["feature entry"]


def test_scaffold_preserves_inline_ownership_in_a_mixed_map(tree: Path) -> None:
    map_path = tree / "feature-map.json"
    entry = {"id": "tick-step-widgets", "origin": "inventory", "tests": ["previous"]}
    map_path.write_text(json.dumps({"features": [entry], "fragments": True}))
    assert scaffold(tree, STEP) == 0
    entries = import_module("scripts.feature_map").load(map_path)["features"]
    assert len(entries) == 1
    assert entries[0]["tests"][0] == "previous"
    assert len(entries[0]["tests"]) == 2
    assert not import_module("scripts.feature_map").entry_path(map_path, "tick-step-widgets").exists()


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
    assert {"inbox relay", "snapshot job", "settle"} <= names


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
    with pytest.raises(SystemExit, match="settle already has"):
        scaffold(tree, "settle")


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
    expected = {
        "step": True,
        "budget row": True,
        "test": True,
        "index row": True,
        "feature entry": True,
    }
    assert capsys.readouterr().out == json.dumps(expected, indent=1) + "\n"
    (tree / "tests/tick/steps/test_probe_widgets.py").unlink()
    assert new_step().main(["--check", STEP, "--root", str(tree)]) == 1
    assert json.loads(capsys.readouterr().out)["test"] is False
    with pytest.raises(ValueError, match="missing behaviour test"):
        import_module("scripts.feature_map").validate(tree, tree / "feature-map.json")


def test_check_reports_nothing_for_an_unknown_step(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert new_step().main(["--check", STEP, "--root", str(tree)]) == 1
    assert set(json.loads(capsys.readouterr().out).values()) == {False}


def test_the_scaffolded_feature_test_passes_against_its_step(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert scaffold(tree, STEP) == 0
    registry = import_module("scripts.tick.registry")
    budgets = import_module("scripts.tick.steps.budgets")
    steps = import_module("scripts.tick.steps")
    monkeypatch.setattr(registry, "registry", registry.Registry())
    monkeypatch.setattr(budgets, "FLOORS", load(tree / "scripts/tick/steps/budgets.py", "scaffolded_budgets").FLOORS)
    monkeypatch.setattr(steps, "__path__", [*steps.__path__, str(tree / "scripts/tick/steps")])
    monkeypatch.delitem(sys.modules, "scripts.tick.steps.widgets", raising=False)
    load(tree / "tests/tick/steps/test_probe_widgets.py", "scaffolded_test").test_probe_widgets()


def test_the_scaffold_formats_under_the_ruff_settings_of_its_tree(tree: Path) -> None:
    (tree / "pyproject.toml").write_text("[tool.ruff]\nline-length = 40\n")
    assert scaffold(tree, STEP) == 0
    assert "def _probe_widgets(\n" in (tree / "scripts/tick/steps/widgets.py").read_text()


def test_the_formatter_refuses_source_ruff_cannot_parse(tree: Path) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        new_step()._formatted(tree / "x.py", "def (\n")


def test_the_scaffold_applies_the_per_file_ruff_settings_of_each_written_file(tree: Path) -> None:
    (tree / "pyproject.toml").write_text('[tool.ruff.lint.per-file-ignores]\n"scripts/tick/steps/*.py" = ["I"]\n')
    assert scaffold(tree, STEP) == 0
    assert (
        "from typing import Any\nfrom scripts.tick import registry\n"
        in (tree / "scripts/tick/steps/widgets.py").read_text()
    )
    assert (
        "import pytest\n\nfrom scripts.tick import registry\n"
        in (tree / "tests/tick/steps/test_probe_widgets.py").read_text()
    )


def test_the_scaffold_lists_the_step_under_the_scenario_given(tree: Path) -> None:
    argv = [STEP, *ARGS, "--feature", "f", "--module", "widgets", "--scenario", "paused", "--root", str(tree)]
    assert new_step().main(argv) == 0
    assert json.loads((tree / "tests/swarm/tick_scenarios/steps.json").read_text())["scenarios"][STEP] == ["paused"]


def test_a_mapped_feature_without_tests_gains_the_new_test_and_keeps_its_text(tree: Path) -> None:
    entry = {"id": "tick-step-widgets", "behaviour": "Widgets counted"}
    (tree / "feature-map.json").write_text(json.dumps({"features": [entry]}))
    assert scaffold(tree, STEP) == 0
    mapped = {"features": [{**entry, "tests": ["tests/tick/steps/test_probe_widgets.py::test_probe_widgets"]}]}
    assert (tree / "feature-map.json").read_text() == json.dumps(mapped, indent=2) + "\n"


def test_the_command_names_itself_and_what_a_step_needs(tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        new_step().main(["--root", str(tree)])
    err = capsys.readouterr().err
    assert err.startswith("usage: python -m scripts.tick.new_step")
    assert err.endswith(
        "python -m scripts.tick.new_step: error: a step needs a name, --group, --lane, --interval, --feature and"
        " --module\n"
    )


@pytest.mark.parametrize(("flag", "value"), [("--lane", "fast"), ("--interval", "90")])
def test_the_scaffold_takes_only_known_lanes_and_intervals(
    tree: Path, capsys: pytest.CaptureFixture[str], flag: str, value: str
) -> None:
    argv = [STEP, *ARGS, "--feature", "f", "--module", "widgets", "--root", str(tree), flag, value]
    with pytest.raises(SystemExit):
        new_step().main(argv)
    assert f"argument {flag}: invalid choice: " in capsys.readouterr().err
    assert not (tree / "scripts/tick/steps/widgets.py").exists()


NEEDS = "a step needs a name, a group and a feature id"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"name": "!!"}, NEEDS),
        ({"group": ""}, NEEDS),
        ({"feature": ""}, NEEDS),
        ({"module": "budgets"}, "budgets is not a steps module name"),
        ({"module": "Widgets"}, "Widgets is not a steps module name"),
        ({"scenario": "nope"}, "unknown scenario nope"),
    ],
)
def test_the_scaffold_refuses_a_step_it_cannot_write(tree: Path, change: dict[str, str], message: str) -> None:
    module = new_step()
    given: dict[str, Any] = {"name": STEP, "group": "widgets", "lane": "tick", "interval": 60, "feature": "f"}
    spec = module.Spec(**{**given, "effects": frozenset(), "module": "widgets", **change})
    with pytest.raises(SystemExit) as refused:
        module.scaffold(tree, spec)
    assert str(refused.value) == message


def test_the_scaffold_names_every_unknown_port_and_every_known_one(tree: Path) -> None:
    module = new_step()
    spec = module.Spec(STEP, "widgets", "tick", 60, frozenset({"disk", "air"}), "f", "widgets")
    with pytest.raises(SystemExit) as refused:
        module.scaffold(tree, spec)
    assert str(refused.value) == f"unknown port air, disk; ports are {', '.join(sorted(module.PORTS))}"


def test_a_step_name_becomes_its_identifier() -> None:
    assert new_step().slug("(Probe  Widgets!)") == "probe_widgets"
    assert new_step().slug("_probe__widgets_") == "probe_widgets"


def test_registered_names_reads_literal_names_of_plain_and_attribute_calls() -> None:
    source = 'CallStep("plain")\nsteps.CallStep("dotted")\nmake()("called")\nCallStep(name)\nprint("other")\n'
    assert new_step().registered_names(source) == {"plain", "dotted"}


def test_missing_imports_land_after_the_last_import_or_the_first_statement() -> None:
    module = new_step()
    block = "".join(line + "\n" for _, line in module.IMPORTS)
    rest = "".join(line + "\n" for name, line in module.IMPORTS if name != "Any")
    imported = '"""Doc."""\nfrom typing import (\n    Any,\n)\nX = 1\n'
    assert module._with_imports('"""Doc."""\nX = 1\n') == '"""Doc."""\n\n' + block + "X = 1\n"
    assert module._with_imports(imported) == '"""Doc."""\nfrom typing import (\n    Any,\n)\n\n' + rest + "X = 1\n"
    assert module._with_imports("# c\n") == "\n" + block + "# c\n"
    assert module._with_imports(block) == block


def test_a_budgets_module_without_its_table_is_refused() -> None:
    module = new_step()
    with pytest.raises(SystemExit) as refused:
        module._table_end("X = 1\n")
    assert str(refused.value) == f"{module.BUDGETS} holds no FLOORS table"


def test_a_new_module_replaces_only_the_modules_tuple(tree: Path) -> None:
    module = new_step()
    (tree / module.INIT).write_text('MODULES = (\n    "a",\n)\nX = 1\n')
    assert module._join_modules(tree, "m") == (tree / module.INIT, 'MODULES = ("a", "scripts.tick.steps.m", )\nX = 1\n')
