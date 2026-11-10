"""Scaffold a tick step with its budget row, feature test, scenario index row and feature map entry.

``python -m scripts.tick.new_step <name> --group <group> --lane tick|slow --interval 60|120|600 --effects <ports>
--feature <id> --module <steps module>`` writes the five artefacts, refuses a name any of them already holds and
formats the Python it wrote. A module the steps package does not hold yet is created and joins
``scripts.tick.steps.MODULES``. ``--check <name>`` prints which of the five artefacts exist and fails on a missing one.
"""

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from scripts import feature_map, tick_pattern
from scripts.tick import catalogue
from scripts.tick.ports.tools import Tools

PACKAGE = "scripts.tick.steps"
STEPS = Path("scripts/tick/steps")
INIT = STEPS / "__init__.py"
BUDGETS = Path(tick_pattern.BUDGETS)
TESTS = Path("tests/tick/steps")
LANES = ("tick", "slow")
INTERVALS = {60: "CRITICAL_S", 120: "WIDE_S", 600: "SLOW_S"}
PORTS = frozenset(field.name for field in fields(Tools))
SCENARIO = "running"
IMPORTS = (
    ("Callable", "from collections.abc import Callable"),
    ("Any", "from typing import Any"),
    ("registry", "from scripts.tick import registry"),
    ("Lane", "from scripts.tick.step import Lane"),
    ("CallStep", "from scripts.tick.steps import CallStep"),
)


@dataclass(frozen=True)
class Spec:
    """One step to scaffold: name, group, lane, interval in seconds, ports, feature id, steps module and scenario."""

    name: str
    group: str
    lane: str
    interval: int
    effects: frozenset[str]
    feature: str
    module: str
    scenario: str = SCENARIO


def slug(name: str) -> str:
    """The identifier form of step ``name``."""
    return "_".join(re.findall(r"[^\W_]+", name.lower()))


def test_path(name: str) -> Path:
    """The feature test file of step ``name``."""
    return TESTS / f"test_{slug(name)}.py"


def test_node(name: str) -> str:
    """The test node id of the feature test of step ``name``."""
    return f"{test_path(name).as_posix()}::test_{slug(name)}"


def _read_json(path: Path) -> Any:
    """The JSON document at ``path``."""
    return json.loads(path.read_text())


def registered_names(source: str) -> set[str]:
    """The literal step names ``source`` registers."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        callee = {getattr(node.func, "id", None), getattr(node.func, "attr", None)}
        first = node.args[0]
        if callee & tick_pattern.POSITIONS.keys() and isinstance(first, ast.Constant) and isinstance(first.value, str):
            found.add(first.value)
    return found


def check(root: Path, name: str) -> dict[str, bool]:
    """Which of the five artefacts of step ``name`` the tree at ``root`` holds."""
    index = _read_json(root / catalogue.INDEX)
    features = feature_map.load(root / catalogue.FEATURE_MAP)["features"]
    return {
        "step": any(name in registered_names(path.read_text()) for path in sorted((root / STEPS).glob("*.py"))),
        "budget row": name in tick_pattern.floors((root / BUDGETS).read_text()),
        "test": (root / test_path(name)).is_file(),
        "index row": name in index["scenarios"] or name in index["unmapped"],
        "feature entry": any(test_node(name) in entry.get("tests", []) for entry in features),
    }


def _refuse(root: Path, spec: Spec) -> None:
    """Refuse a step whose name, ports, module or scenario the scaffold cannot write, or whose name is taken."""
    unknown = sorted(spec.effects - PORTS)
    if unknown:
        raise SystemExit(f"unknown port {', '.join(unknown)}; ports are {', '.join(sorted(PORTS))}")
    if not slug(spec.name) or not spec.group or not spec.feature:
        raise SystemExit("a step needs a name, a group and a feature id")
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", spec.module) or spec.module == "budgets":
        raise SystemExit(f"{spec.module} is not a steps module name")
    if spec.scenario not in catalogue.scenarios(root):
        raise SystemExit(f"unknown scenario {spec.scenario}")
    found = [artefact for artefact, present in check(root, spec.name).items() if present]
    if found:
        raise SystemExit(f"{spec.name} already has {', '.join(found)}")


def _set_literal(items: frozenset[str]) -> str:
    """Source for a frozenset of ``items``."""
    return f"frozenset({{{', '.join(json.dumps(item) for item in sorted(items))}}})" if items else "frozenset()"


def _stub(spec: Spec) -> str:
    """Source of the step function and its registration."""
    call = slug(spec.name)
    return (
        f"def _{call}(tick: Any, get: Callable[[str], Any]) -> list[str]:\n"
        f'    """Return the actions of the {spec.name} step, given the tick module and a reader of the pass state."""\n'
        "    return []\n\n\n"
        "registry.register(\n"
        "    CallStep(\n"
        f"        {json.dumps(spec.name)},\n"
        f"        {json.dumps(spec.group)},\n"
        f"        _{call},\n"
        "        needs=frozenset(),\n"
        "        reads=frozenset(),\n"
        "        writes=frozenset(),\n"
        f"        lane=Lane({json.dumps(spec.lane)}),\n"
        f"        effects={_set_literal(spec.effects)},\n"
        f"        feature={json.dumps(spec.feature)},\n"
        "    )\n"
        ")\n"
    )


def _bound(tree: ast.Module) -> set[str]:
    """The names the top level imports of ``tree`` bind."""
    return {
        alias.asname or alias.name
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }


def _with_imports(source: str) -> str:
    """``source`` with each import the step stub needs and it lacks, after its last top level import."""
    tree = ast.parse(source)
    missing = [line + "\n" for name, line in IMPORTS if name not in _bound(tree)]
    anchors = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))] or tree.body[:1]
    at = max((node.end_lineno or node.lineno for node in anchors), default=0)
    lines = source.splitlines(keepends=True)
    inserted = ["\n", *missing] if missing else []
    return "".join([*lines[:at], *inserted, *lines[at:]])


def _step_text(root: Path, spec: Spec) -> tuple[Path, str]:
    """The steps module with the step stub appended, created when the package does not hold it."""
    path = root / STEPS / f"{spec.module}.py"
    source = path.read_text() if path.is_file() else f'"""The {spec.module} steps of the tick."""\n'
    return path, _with_imports(source) + "\n\n" + _stub(spec)


def _table_end(source: str) -> int:
    """The line of the closing brace of the budgets table."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Assign):
            targets = node.targets
        else:
            continue
        if node.value and any(isinstance(target, ast.Name) and target.id == "FLOORS" for target in targets):
            return node.value.end_lineno or node.value.lineno
    raise SystemExit(f"{BUDGETS} holds no FLOORS table")


def _row_text(root: Path, spec: Spec) -> tuple[Path, str]:
    """The budgets module with the step's light floor row at the end of its table."""
    path = root / BUDGETS
    source = path.read_text()
    lines = source.splitlines(keepends=True)
    row = f"    {json.dumps(spec.name)}: Timing(LIGHT_S, {INTERVALS[spec.interval]}),\n"
    lines.insert(_table_end(source) - 1, row)
    return path, "".join(lines)


def _test_source(spec: Spec) -> str:
    """Source of the step's feature test."""
    expected = ", ".join(json.dumps(value) for value in (spec.group, spec.lane, spec.feature))
    return (
        f'"""The {spec.name} tick step registers with its group, lane, feature and ports."""\n\n'
        "from importlib import import_module\n\n"
        "import pytest\n\n"
        "from scripts.tick import registry\n\n\n"
        f"@pytest.mark.feature({json.dumps(spec.feature)})\n"
        f"def test_{slug(spec.name)}() -> None:\n"
        f'    """The {spec.name} step registers in group {spec.group} on the {spec.lane} lane with its feature and ports."""\n'
        f"    import_module({json.dumps(f'{PACKAGE}.{spec.module}')})\n"
        f"    found = {{step.name: step for step in registry.steps()}}[{json.dumps(spec.name)}]\n"
        "    assert (found.group, found.lane, found.feature, found.effects) == (\n"
        f"        {expected}, {_set_literal(spec.effects)}\n"
        "    )\n"
    )


def _test_text(root: Path, spec: Spec) -> tuple[Path, str]:
    """The step's feature test file and its source."""
    path = root / test_path(spec.name)
    return path, _test_source(spec)


def _index_text(root: Path, spec: Spec) -> tuple[Path, str]:
    """The scenario index with the step listed under its scenario."""
    path = root / catalogue.INDEX
    index = _read_json(path)
    index["scenarios"] = dict(sorted({**index["scenarios"], spec.name: [spec.scenario]}.items()))
    return path, json.dumps(index, indent=1) + "\n"


def _feature_text(root: Path, spec: Spec) -> tuple[Path, str]:
    """The feature map with the step's test added to its entry, a new inventory entry for a new id."""
    path = root / catalogue.FEATURE_MAP
    legacy = _read_json(path)
    fragmented = legacy.get("fragments") and not any(entry["id"] == spec.feature for entry in legacy["features"])
    data = feature_map.load(path) if fragmented else legacy
    entry = next((entry for entry in data["features"] if entry["id"] == spec.feature), None)
    if entry is None:
        data["features"].append(
            {
                "id": spec.feature,
                "origin": "inventory",
                "behaviour": f"The tick step {spec.name} runs in group {spec.group} on the {spec.lane} lane "
                f"every {spec.interval} seconds.",
                "seam": f"{PACKAGE}.{spec.module}._{slug(spec.name)} registered as tick step {spec.name}",
                "tests": [test_node(spec.name)],
            }
        )
    else:
        entry["tests"] = [*entry.get("tests", []), test_node(spec.name)]
    if fragmented:
        entry = next(entry for entry in data["features"] if entry["id"] == spec.feature)
        return feature_map.entry_path(path, spec.feature), json.dumps(entry, indent=2) + "\n"
    return path, json.dumps(data, indent=2) + "\n"


def _join_modules(root: Path, module: str) -> tuple[Path, str]:
    """The steps package with ``module`` appended to its ``MODULES`` tuple."""
    path = root / INIT
    source = path.read_text()
    value = next(
        node.value
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "MODULES" for t in node.targets)
    )
    names = [*ast.literal_eval(value), f"{PACKAGE}.{module}"]
    lines = source.splitlines(keepends=True)
    first, last = value.lineno - 1, (value.end_lineno or value.lineno) - 1
    head, tail = lines[first][: value.col_offset], lines[last][value.end_col_offset :]
    lines[first : last + 1] = [head + "(" + "".join(f"{json.dumps(name)}, " for name in names) + ")" + tail]
    return path, "".join(lines)


def _formatted(path: Path, text: str) -> str:
    """``text`` of the Python file at ``path`` with its imports sorted and formatted under the ruff settings that
    apply to ``path``, read and returned on stdin and stdout so nothing is written."""
    named = ["--stdin-filename", str(path)]
    for command in (["check", "--select", "I", "--fix", *named], ["format", *named]):
        text = subprocess.run(
            [sys.executable, "-m", "ruff", *command], input=text, capture_output=True, text=True, check=True
        ).stdout
    return text


def scaffold(root: Path, spec: Spec) -> list[Path]:
    """Write the five artefacts of ``spec`` in the tree at ``root`` and return the files written."""
    _refuse(root, spec)
    new = not (root / STEPS / f"{spec.module}.py").is_file()
    texts = [
        _step_text(root, spec),
        _row_text(root, spec),
        _test_text(root, spec),
        _index_text(root, spec),
        _feature_text(root, spec),
    ]
    if new:
        texts.append(_join_modules(root, spec.module))
    final = [(path, _formatted(path, text) if path.suffix == ".py" else text) for path, text in texts]
    for path, text in final:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return [path for path, _ in final]


def _parser() -> argparse.ArgumentParser:
    """The command line of the scaffold."""
    parser = argparse.ArgumentParser(prog="python -m scripts.tick.new_step")
    parser.add_argument("name", nargs="?")
    parser.add_argument("--check")
    parser.add_argument("--group")
    parser.add_argument("--lane", choices=LANES)
    parser.add_argument("--interval", type=int, choices=sorted(INTERVALS))
    parser.add_argument("--effects", default="")
    parser.add_argument("--feature")
    parser.add_argument("--module")
    parser.add_argument("--scenario", default=SCENARIO)
    parser.add_argument("--root", default=".")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Scaffold a step, or report which artefacts of a step exist with ``--check``."""
    parser = _parser()
    args = parser.parse_args(argv)
    root = Path(args.root)
    if args.check:
        found = check(root, args.check)
        print(json.dumps(found, indent=1))
        return 0 if all(found.values()) else 1
    if not all((args.name, args.group, args.lane, args.interval, args.feature, args.module)):
        parser.error("a step needs a name, --group, --lane, --interval, --feature and --module")
    effects = frozenset(port.strip() for port in args.effects.split(",") if port.strip())
    spec = Spec(args.name, args.group, args.lane, args.interval, effects, args.feature, args.module, args.scenario)
    for path in scaffold(root, spec):
        print(path.relative_to(root).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
