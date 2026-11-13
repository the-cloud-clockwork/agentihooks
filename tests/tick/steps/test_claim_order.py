"""The claim order cluster and launch records live in the claims step module and the swarm tick module re-exports
each name."""

from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest

from scripts.swarm import store as swarm_store
from scripts.swarm import tick
from scripts.swarm.store import AgentRecord
from scripts.tick.steps import claims

pytestmark = pytest.mark.unit

CLAIMED = (
    "SpawnError",
    "Placed",
    "Ledger",
    "Runtime",
    "agent_status",
    "ready_in_order",
    "ready_by_lane",
    "_owned_tasks",
    "_claimable",
    "Claims",
    "claim_view",
    "_launch_order",
    "_demoted",
)
MOVED = [(claims, name) for name in CLAIMED]


@pytest.mark.feature("swarm-tick-claim-order")
@pytest.mark.parametrize(("module", "name"), MOVED)
def test_tick_reexports_the_moved_name(module: ModuleType, name: str) -> None:
    moved = getattr(module, name)
    assert moved.__module__ == module.__name__
    assert getattr(tick, name.removeprefix("_") if name == "_run_group" else name) is moved


@pytest.mark.feature("swarm-tick-claim-order")
def test_the_tick_module_holds_no_moved_definition() -> None:
    text = Path(str(tick.__file__)).read_text(encoding="utf-8")
    held = [name for _, name in MOVED if f"\ndef {name}(" in text or f"\nclass {name}" in text]
    assert held == []


@pytest.mark.feature("swarm-tick-claim-order")
def test_moved_claim_code_reaches_tick_seams_through_the_tick_module(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Any, ...]] = []

    def claimable(*args: Any) -> list[str]:
        calls.append(("claimable", *args))
        return ["t1"]

    def launch_order(*args: Any) -> list[str]:
        calls.append(("order", *args))
        return ["ordered"]

    monkeypatch.setattr(tick, "_claimable", claimable)
    monkeypatch.setattr(tick, "_launch_order", launch_order)
    assert claims.ready_in_order("sw", "store", "rows", "doc", "eng") == ["ordered"]
    assert calls == [
        ("claimable", "sw", "store", "rows", "doc", "eng", None, None),
        ("order", "sw", "store", ["t1"], None),
    ]
    calls.clear()
    assert claims.ready_in_order("sw", "store", "rows", "doc", "ci", "dispatch", "claims") == ["ordered"]
    assert calls == [
        ("claimable", "sw", "store", "rows", "doc", "ci", "dispatch", "claims"),
        ("order", "sw", "store", ["t1"], "dispatch"),
    ]


@pytest.mark.feature("swarm-spawn-order-once-per-pass")
@pytest.mark.parametrize("given", [None, "claims"])
def test_ready_by_lane_orders_every_lane_from_one_claim_view(monkeypatch: pytest.MonkeyPatch, given: Any) -> None:
    calls: list[tuple[Any, ...]] = []

    def view(*args: Any) -> str:
        calls.append(("view", *args))
        return "built"

    def claimable(*args: Any) -> list[dict[str, str]]:
        calls.append(("claimable", *args))
        return [{"id": f"{args[4]}1", "lane": args[4]}]

    def launch_order(*args: Any) -> list[dict[str, str]]:
        calls.append(("order", *args))
        return list(reversed(args[2]))

    monkeypatch.setattr(tick, "claim_view", view)
    monkeypatch.setattr(tick, "_claimable", claimable)
    monkeypatch.setattr(tick, "_launch_order", launch_order)
    found = claims.ready_by_lane(
        "sw", cast(Any, "store"), cast(Any, "rows"), cast(Any, "doc"), ["eng", "ci"], cast(Any, "key"), given
    )
    used = given or "built"
    assert calls == [
        *([] if given else [("view", "sw", "store", "rows", "doc", "key")]),
        ("claimable", "sw", "store", "rows", "doc", "eng", "key", used),
        ("claimable", "sw", "store", "rows", "doc", "ci", "key", used),
        ("order", "sw", "store", [{"id": "eng1", "lane": "eng"}, {"id": "ci1", "lane": "ci"}], "key"),
    ]
    assert found == {"eng": [{"id": "eng1", "lane": "eng"}], "ci": [{"id": "ci1", "lane": "ci"}]}


@pytest.mark.feature("swarm-tick-claim-order")
@pytest.mark.parametrize(
    ("fields", "status"),
    [
        ({"state": "finished"}, "finished"),
        ({"state": "awaiting-decision"}, "awaiting-decision"),
        ({"input_prompt": "y/n"}, "waiting"),
        ({"idle_ticks": 3}, "stalled"),
        ({"idle_ticks": 2}, "idle"),
        ({}, "working"),
    ],
)
def test_agent_status_reads_state_prompt_and_idle_ticks(fields: dict[str, Any], status: str) -> None:
    agent = AgentRecord("engineer@1-1", "eng", "t1", **fields)
    assert claims.agent_status(agent) == status


@pytest.mark.feature("swarm-tick-claim-order")
def test_the_store_projects_agent_status_from_the_claims_module() -> None:
    text = Path(str(swarm_store.__file__)).read_text(encoding="utf-8")
    assert "from scripts.tick.steps.claims import agent_status" in text
    assert "from scripts.swarm.tick import agent_status" not in text


@pytest.mark.feature("swarm-tick-claim-order")
def test_a_spawn_error_defaults_to_a_refusal_with_no_pane_or_harness() -> None:
    plain = claims.SpawnError("no room")
    assert (str(plain), plain.status, plain.pane_id, plain.harness) == ("no room", "refused", "", "")
    named = claims.SpawnError("pending", "pending", "p1", harness="codex")
    assert (named.status, named.pane_id, named.harness) == ("pending", "p1", "codex")


def _task(task_id: str, territory: list[str] | None = None, **fields: Any) -> dict[str, Any]:
    return {"id": task_id, **({"territory": territory} if territory is not None else {}), **fields}


@pytest.mark.feature("swarm-tick-claim-order")
def test_claimable_puts_tasks_overlapping_held_territory_after_the_clear_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, str, object]] = []

    def ready(task: dict[str, Any], lane: str, view: object) -> bool:
        seen.append((task["id"], lane, view))
        return task["id"] != "t4"

    monkeypatch.setattr(tick.readiness, "claimable", ready)
    ordered = [
        _task("t1", ["held/x"]),
        _task("t2", ["b"]),
        _task("t3", ["b/y"]),
        _task("t4", ["c"]),
        _task("t5", ["d"]),
        _task("t6"),
    ]
    view: Any = claims.Claims(cast(Any, "view"), [["held"]], ordered)
    found = claims._claimable("sw", "store", {}, {}, "eng", None, view)
    assert [t["id"] for t in found] == ["t2", "t5", "t6", "t1", "t3"]
    assert seen == [(t["id"], "eng", "view") for t in ordered]
    assert view.held == [["held"]]


@pytest.mark.feature("swarm-tick-claim-order")
def test_claimable_builds_its_claim_view_with_the_dispatch_order(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[tuple[Any, ...]] = []

    def order(task: dict[str, Any]) -> tuple[int]:
        return (0,)

    def view(*args: Any) -> Any:
        built.append(args)
        return claims.Claims(cast(Any, "view"), [], [])

    monkeypatch.setattr(tick, "claim_view", view)
    assert claims._claimable("sw", "store", {"r": {}}, {"d": 1}, "eng", order) == []
    assert built == [("sw", "store", {"r": {}}, {"d": 1}, order)]


@pytest.mark.feature("swarm-tick-claim-order")
def test_launch_order_ranks_then_demotes_failed_launches_then_resumes_first(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[tuple[Any, ...]] = []

    def demoted(slug: str, store: object, task_ids: list[str]) -> set[str]:
        asked.append((slug, store, task_ids))
        return {"b"}

    monkeypatch.setattr(tick, "_demoted", demoted)
    monkeypatch.setattr(tick.ledger_rank, "order", lambda task: task["rank"])
    monkeypatch.setattr(tick.claim_order, "resumed", lambda task: task["resumed"])
    tasks = [
        _task("a", rank=1, resumed=False),
        _task("b", rank=1, resumed=True),
        _task("c", rank=1, resumed=True),
        _task("d", rank=0, resumed=False),
    ]
    assert [t["id"] for t in claims._launch_order("sw", "store", tasks)] == ["d", "c", "a", "b"]
    assert asked == [("sw", "store", ["a", "b", "c", "d"])]
    dispatched = [t["id"] for t in claims._launch_order("sw", "store", tasks, lambda task: (-task["rank"],))]
    assert dispatched == ["c", "a", "b", "d"]


class _LaunchStore:
    """A store answering launch failures and handoff tasks and recording each handoff ask."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, list[str]]] = []

    def launch_failures(self, slug: str) -> dict[str, int]:
        return {"a": 1, "b": 2, "c": 0} if slug == "sw" else {}

    def handoff_tasks(self, slug: str, task_ids: list[str]) -> set[str]:
        self.asked.append((slug, task_ids))
        return {"b"}


@pytest.mark.feature("swarm-tick-claim-order")
def test_demoted_names_failed_launches_without_a_handoff() -> None:
    store: Any = _LaunchStore()
    assert claims._demoted("sw", store, ["a", "b", "c", "d"]) == {"a"}
    assert store.asked == [("sw", ["a", "b"])]
    assert claims._demoted("sw", store, ["c", "d"]) == set()
    assert store.asked == [("sw", ["a", "b"])]


class _ViewStore:
    """A store answering the claim view's reads."""

    def agents(self, slug: str) -> list[AgentRecord]:
        return [AgentRecord("engineer@1-1", "eng", "owned")]

    def claimed(self, slug: str, task_ids: list[str]) -> set[str]:
        return {"held"} if task_ids == ["open1", "held"] else set()

    def config(self, slug: str) -> str:
        return "config"


@pytest.mark.feature("swarm-tick-claim-order")
def test_claim_view_holds_active_territories_and_marks_owned_or_claimed_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots: list[tuple[Any, ...]] = []
    monkeypatch.setattr(tick.hierarchy, "project", lambda doc: ("graph", "rest"))
    monkeypatch.setattr(tick.freeze, "fix_phase", lambda config: f"fix {config}")
    built: list[tuple[Any, ...]] = []

    def released(config: str) -> Any:
        built.append(("released", config))
        return lambda task: task["id"] != "r"

    def applied(config: str, rows: object) -> Any:
        built.append(("applied", config, rows))
        return lambda task: task["id"] != "a"

    def reader(store: object, slug: str) -> str:
        built.append(("reader", store, slug))
        return "reader"

    def unshared(saved: str) -> Any:
        built.append(("unshared", saved))
        return lambda task: task["id"] != "f"

    monkeypatch.setattr(tick.release_wait, "checker", released)
    monkeypatch.setattr(tick.apply_wait, "checker", applied)
    monkeypatch.setattr(tick.fleet_red, "saved_reader", reader)
    monkeypatch.setattr(tick.fleet_red, "checker", unshared)
    monkeypatch.setattr(tick.readiness, "Snapshot", lambda *args: snapshots.append(args) or "snapshot")
    rows = {
        "open1": _task("open1", state="open", order=3),
        "held": _task("held", state="open", order=1),
        "c1": _task("c1", ["x"], state="claimed", order=2),
        "p1": _task("p1", state="pr", order=0),
        "d1": _task("d1", ["y"], state="done", order=4),
    }
    store: Any = _ViewStore()
    view = claims.claim_view("sw", store, rows, {"doc": 1}, lambda task: (task["order"],))
    assert view.view == "snapshot"
    assert view.held == [["x"], []]
    assert [t["id"] for t in view.ordered] == ["p1", "held", "c1", "open1", "d1"]
    slug, seen_rows, doc, graph, fix, owned, passes = snapshots[0]
    assert (slug, seen_rows, doc, graph, fix) == ("sw", rows, {"doc": 1}, "graph", "fix config")
    assert [owned(t) for t in ("owned", "held", "open1")] == [True, True, False]
    assert [passes({"id": t}) for t in ("ok", "r", "a", "f")] == [True, False, False, False]
    assert built == [
        ("released", "config"),
        ("applied", "config", rows),
        ("reader", store, "sw"),
        ("unshared", "reader"),
    ]
