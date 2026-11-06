"""The claim order cluster and launch records live in the claims step module and the swarm tick module re-exports
each name."""

from pathlib import Path
from types import ModuleType
from typing import Any

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
