"""Every feature whose tests reach the legacy tick spawn keeps events path coverage, retires with it or names its gap."""

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.feature_map import load

ROOT = Path(__file__).resolve().parents[2]
COVERAGE = ROOT / "tests" / "LEGACY_SPAWN_COVERAGE.json"
MODULES = frozenset({"scripts.swarm.launch_job"})
OWNED = {
    "scripts.tick.steps.place": frozenset(
        {
            "_spawn_step",
            "_spawn",
            "_prepare",
            "_launch",
            "_launch_all",
            "_retry_successor",
            "_spawn_stop",
            "_release_hive_launch",
            "_settle_admission",
            "_release_admission",
            "_placement_pass",
            "_admission",
            "_room_wait",
            "_held_back",
        }
    ),
    "scripts.tick.steps.observe": frozenset(
        {
            "step_spawn_due",
            "_collect_acks",
            "_expire_launch",
            "_failed_hive_launch",
            "_finished_launch",
            "_acknowledging",
        }
    ),
    "scripts.hive.launch": frozenset({"collect", "expire"}),
}
TICKS = frozenset({"run_tick", "tick"})
SPAWNS = frozenset({"spawned", "spawns"})
KINDS = frozenset({"covered_by", "retires", "gap"})

Function = ast.FunctionDef | ast.AsyncFunctionDef


def _imports(tree: ast.Module) -> tuple[set[str], dict[str, frozenset[str]]]:
    """Names bound to a removed module or symbol, and names bound to a module whose listed members are removed."""
    whole: set[str] = set()
    owners: dict[str, frozenset[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name
                if alias.name in MODULES:
                    whole.add(bound)
                if alias.name in OWNED:
                    owners[bound] = OWNED[alias.name]
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                bound, full = alias.asname or alias.name, f"{node.module}.{alias.name}"
                if full in MODULES or alias.name in OWNED.get(node.module, frozenset()):
                    whole.add(bound)
                elif full in OWNED:
                    owners[bound] = OWNED[full]
    return whole, owners


def _direct(node: Function, whole: set[str], owners: dict[str, frozenset[str]]) -> bool:
    """True when the function body names a removed symbol or asserts a tick spawn outside the events engine."""
    names = {found.id for found in ast.walk(node) if isinstance(found, ast.Name)}
    attributes = [found for found in ast.walk(node) if isinstance(found, ast.Attribute)]
    if names & whole or any(found.attr == "launch_job" for found in attributes):
        return True
    if any(found.attr in owners.get(ast.unparse(found.value), frozenset()) for found in attributes):
        return True
    calls = {ast.unparse(found.func).rsplit(".", 1)[-1] for found in ast.walk(node) if isinstance(found, ast.Call)}
    events = "EVENTS_ENGINE" in names or any(
        isinstance(found, ast.Constant) and found.value == "events" for found in ast.walk(node)
    )
    return bool(calls & TICKS) and not events and any(found.attr in SPAWNS for found in attributes)


def reaching(source: str) -> dict[str, bool]:
    """Each function of a test module and whether it reaches the legacy spawn, directly or through module helpers."""
    tree = ast.parse(source)
    whole, owners = _imports(tree)
    functions: dict[str, Function] = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    known: dict[str, bool] = {}

    def reaches(name: str, stack: frozenset[str]) -> bool:
        if name in known:
            return known[name]
        if name in stack:
            return False
        node = functions[name]
        used = {found.id for found in ast.walk(node) if isinstance(found, ast.Name)}
        used |= {found.arg for found in ast.walk(node) if isinstance(found, ast.arg)}
        helpers = (other for other in used if other in functions and other != name)
        known[name] = _direct(node, whole, owners) or any(reaches(other, stack | {name}) for other in helpers)
        return known[name]

    return {name: reaches(name, frozenset()) for name in functions}


def _reaches(node_id: str, cache: dict[str, dict[str, bool]]) -> bool:
    path, _, name = node_id.partition("::")
    if path not in cache:
        cache[path] = reaching((ROOT / path).read_text())
    return cache[path].get(name.rsplit("::", 1)[-1].split("[")[0], False)


def _features() -> list[dict[str, Any]]:
    return load(ROOT / "feature-map.json")["features"]


def _coverage() -> dict[str, dict[str, Any]]:
    return json.loads(COVERAGE.read_text())["features"]


@pytest.mark.feature("legacy-spawn-events-coverage")
def test_every_feature_whose_tests_reach_the_legacy_spawn_is_in_the_coverage_map() -> None:
    cache: dict[str, dict[str, bool]] = {}
    reached = {entry["id"] for entry in _features() if any(_reaches(node, cache) for node in entry["tests"])}
    assert sorted(reached - set(_coverage())) == []


@pytest.mark.feature("legacy-spawn-events-coverage")
def test_every_coverage_entry_names_mapped_tests_off_the_legacy_spawn_a_retirement_or_a_gap() -> None:
    features = {entry["id"]: entry for entry in _features()}
    owned = {node for entry in features.values() for node in entry["tests"]}
    cache: dict[str, dict[str, bool]] = {}
    for feature, entry in _coverage().items():
        assert feature in features, feature
        assert len(entry) == 1 and set(entry) <= KINDS, feature
        assert entry.get("covered_by", True), feature
        assert str(entry.get("retires", entry.get("gap", "covered"))).strip(), feature
        for node in entry.get("covered_by", []):
            assert node in owned, node
            assert not _reaches(node, cache), node


@pytest.mark.feature("legacy-spawn-events-coverage")
@pytest.mark.parametrize(
    "source, expected",
    [
        ("from scripts.swarm import launch_job\ndef test_a():\n    launch_job.collect(1)\n", True),
        ("from scripts.tick.steps import place\ndef test_a():\n    place._spawn_step(1, 2)\n", True),
        ("from scripts.tick.steps import place\ndef test_a():\n    place.primed(1, 2, 3, 4)\n", False),
        ("from scripts.hive.launch import collect\ndef test_a():\n    collect(1)\n", True),
        ("def helper(r):\n    return r.spawned\ndef test_a(r):\n    tick(1)\n    helper(r)\n", False),
        ("def test_a(r):\n    tick(1)\n    assert r.spawned\n", True),
        ("def test_a(r, s):\n    s.update(engine='events')\n    tick(1)\n    assert r.spawned == []\n", False),
        (
            "from scripts.tick.steps import place\ndef helper():\n    place._prepare(1)\ndef test_a():\n    helper()\n",
            True,
        ),
    ],
)
def test_the_detector_finds_legacy_spawn_reach_and_clears_the_rest(source: str, expected: bool) -> None:
    assert reaching(source)["test_a"] is expected
