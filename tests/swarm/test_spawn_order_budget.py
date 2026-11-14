"""The spawn order computes its claim order once per pass and fits its budget over a replayed swarm ledger."""

import gzip
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.swarm import claim_hold, fleet_red, freeze, release_wait
from scripts.swarm import tick as tick_module
from scripts.swarm_ledger.repository import hierarchy
from tests.swarm import test_tick

pytestmark = pytest.mark.unit
store = test_tick.store
FakeLedger, FakeRuntime = test_tick.FakeLedger, test_tick.FakeRuntime
REPLAY = Path(__file__).with_name("spawn_pass_replay.json.gz")
BUDGET_SHARE = 10


def replay() -> dict[str, Any]:
    """A swarm ledger's structure: 4827 tasks, 997 open, 1322 freeze and focus records."""
    with gzip.open(REPLAY, "rt") as raw:
        return json.load(raw)


def counted(monkeypatch: pytest.MonkeyPatch, name: str) -> list[Any]:
    """Count every call of the swarm tick function ``name`` while still running it."""
    calls: list[Any] = []
    real = getattr(tick_module, name)
    monkeypatch.setattr(tick_module, name, lambda *args: calls.append(args) or real(*args))
    return calls


def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the claim filters off the network: no release tags and no open pull request listings."""
    monkeypatch.setattr(release_wait, "tags", lambda config: [])
    monkeypatch.setattr(fleet_red, "read", lambda repo, *args, **kwargs: ())


def scanned(records: list[dict[str, Any]], verb: str, task: dict[str, Any], chain: list[str]) -> int:
    """The nearest covering level as a scan over every record finds it."""
    targets = [r["target"] for r in records if r["verb"] == verb]
    levels = [chain.index(t) if t in chain else len(chain) for t in targets if freeze.covers(t, task, chain)]
    return min(levels, default=sys.maxsize)


@pytest.mark.feature("swarm-spawn-order-once-per-pass")
@pytest.mark.parametrize("verb", ["freeze", "focus"])
def test_the_indexed_freeze_targets_find_the_level_a_full_scan_finds(verb: str) -> None:
    other = "freeze" if verb == "focus" else "focus"
    targets = ("lane:eng", "kind:ops", "lane:", "kind", "tasks/t1", "phases/p1", "plans/q", "lane:ci", "lane:eng:x")
    records = [{"verb": verb, "target": target} for target in targets] + [{"verb": other, "target": "tasks/t2"}]
    tasks = [
        ({"id": "t1", "lane": "eng"}, ["tasks/t1", "slices/s", "phases/p1", "plans/q"]),
        ({"id": "t2", "lane": "plan", "kind": "ops"}, ["tasks/t2", "phases/p2"]),
        ({"id": "t3", "lane": "plan"}, ["tasks/t3", "lane:", "phases/p1"]),
        ({"id": "t4", "lane": "ci"}, ["tasks/t4"]),
        ({"id": "t5", "lane": "plan"}, ["tasks/t5", "phases/p9"]),
        ({"id": "t6", "lane": "plan"}, ["tasks/t6", "lane:eng:x"]),
    ]
    for task, chain in tasks:
        assert freeze._nearest(records, verb, task, chain) == scanned(records, verb, task, chain)


@pytest.mark.feature("swarm-spawn-order-once-per-pass")
def test_the_freeze_index_is_built_once_per_freeze_list() -> None:
    records = [{"verb": "freeze", "target": "tasks/t1"}, {"verb": "focus", "target": "lane:eng"}]
    first = freeze._targets(records)
    assert freeze._targets(records) is first
    assert first == {"freeze": (frozenset({"tasks/t1"}), ()), "focus": (frozenset(), (("lane", "eng"),))}
    assert freeze._targets([*records]) is not first


@pytest.mark.feature("swarm-spawn-order-once-per-pass")
def test_the_readiness_hold_keeps_the_fix_phase_and_open_dependencies() -> None:
    task = {"id": "t1", "state": "open", "lane": "eng", "phase": "fix"}
    other = {"id": "o1", "state": "open", "lane": "eng", "phase": "work"}
    doc = {"tasks": [task, other], "phases": [], "freezes": [{"verb": "focus", "target": "tasks/o1"}]}
    rows = {row["id"]: row for row in doc["tasks"]}
    graph = hierarchy.project(doc)[0]
    assert claim_hold.holds(task, rows, doc, graph, "fix") is False
    assert claim_hold.holds(task, rows, doc, graph, "") is True
    waiting = {"id": "t2", "state": "open", "lane": "eng", "depends_on": ["d1"]}
    plain = {"tasks": [waiting, {"id": "d1", "state": "open"}], "phases": []}
    plain_rows = {row["id"]: row for row in plain["tasks"]}
    assert claim_hold.holds(waiting, plain_rows, plain, hierarchy.project(plain)[0], "") is True
    assert claim_hold.holds({"id": "t3", "state": "open"}, plain_rows, plain, {}, "") is False


@pytest.mark.feature("swarm-spawn-order-once-per-pass")
@pytest.mark.parametrize("prebuilt", [True, False])
def test_ready_by_lane_passes_one_claim_view_and_its_pass_inputs_to_every_lane(
    monkeypatch: pytest.MonkeyPatch, prebuilt: bool
) -> None:
    calls: list[tuple[str, tuple[Any, ...]]] = []
    built: Any = object()
    given: Any = object()
    tasks = {"eng": [{"id": "e1", "lane": "eng"}], "ci": [{"id": "c1", "lane": "ci"}]}

    def view(*args: Any) -> object:
        calls.append(("view", args))
        return built

    def claimable(*args: Any) -> list[dict[str, Any]]:
        calls.append(("claimable", args))
        return tasks[args[4]]

    def launch(*args: Any) -> list[dict[str, Any]]:
        calls.append(("launch", args))
        return list(reversed(args[2]))

    monkeypatch.setattr(tick_module, "claim_view", view)
    monkeypatch.setattr(tick_module, "_claimable", claimable)
    monkeypatch.setattr(tick_module, "_launch_order", launch)
    store_: Any = object()
    key: Any = len
    rows: dict[str, dict[str, Any]] = {"r": {}}
    doc: dict[str, Any] = {"d": 1}
    found = tick_module.ready_by_lane("sw", store_, rows, doc, ["eng", "ci"], key, given if prebuilt else None)
    claims = given if prebuilt else built
    expected = [] if prebuilt else [("view", ("sw", store_, rows, doc, key))]
    expected += [("claimable", ("sw", store_, rows, doc, lane, key, claims)) for lane in ("eng", "ci")]
    expected += [("launch", ("sw", store_, [tasks["eng"][0], tasks["ci"][0]], key))]
    assert calls == expected
    assert found == {"eng": tasks["eng"], "ci": tasks["ci"]}
