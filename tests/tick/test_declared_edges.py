"""Site edges from declared values: a step of a site waits only for the earlier steps whose values it uses."""

from dataclasses import dataclass
from typing import Any

import pytest

import scripts.swarm.cli
from scripts.tick import engine, registry
from scripts.tick.sites import CONFIG, TICK_SITES, TickSites
from scripts.tick.step import Context, Domain, Lane, Result

pytestmark = pytest.mark.unit

ROWS, DOC, FINISHED = Domain("pass:rows"), Domain("pass:doc"), Domain("pass:finished")


@dataclass
class Step:
    name: str
    group: str
    needs: frozenset[str] = frozenset()
    reads: frozenset[Domain] = frozenset()
    writes: frozenset[Domain] = frozenset()
    lane: Lane = Lane("critical")
    budget_s: float = 1.0
    interval_s: float = 0.0
    effects: frozenset[str] = frozenset()
    feature: str = ""
    kept: bool = False

    def run(self, ctx: Context) -> Result:
        return Result()


def _needs(steps: tuple[Any, ...]) -> dict[str, frozenset[str]]:
    return {step.name: step.needs for step in steps}


def _pass_needs() -> dict[str, frozenset[str]]:
    sites = scripts.swarm.cli.PASS_SITES
    order = {site: index for index, site in enumerate(sites)}
    found = sorted((s for s in registry.steps() if s.group in order), key=lambda s: order[s.group])
    edged = registry.site_edges(found, sites, scripts.swarm.cli.PASS_HOOK_KEYS, scripts.swarm.cli.PASS_SHUT_KEYS)
    return _needs(edged)


@pytest.mark.feature("swarm-tick-declared-edges")
def test_a_step_waits_only_for_the_earlier_steps_whose_values_it_uses() -> None:
    writer = Step("writer", "a", writes=frozenset({ROWS}))
    reader = Step("reader", "a", reads=frozenset({DOC}))
    after = Step("after", "b", reads=frozenset({ROWS}), writes=frozenset({DOC}))
    free = Step("free", "b")
    edged = registry.site_edges((writer, reader, after, free), ("a", "b"))
    assert _needs(edged) == {
        "writer": frozenset(),
        "reader": frozenset(),
        "after": frozenset({"writer", "reader"}),
        "free": frozenset(),
    }


@pytest.mark.feature("swarm-tick-declared-edges")
def test_a_hooked_site_and_every_later_site_wait_for_the_writers_of_the_hook_values() -> None:
    writer = Step("writer", "a", writes=frozenset({DOC}))
    other = Step("other", "a", writes=frozenset({ROWS}))
    hooked = Step("hooked", "b")
    later = Step("later", "c")
    hooks = {"b": (frozenset(), frozenset({Domain("hook:doc")}))}
    edged = registry.site_edges((writer, other, hooked, later), ("a", "b", "c"), hooks)
    assert _needs(edged) == {
        "writer": frozenset(),
        "other": frozenset(),
        "hooked": frozenset({"writer"}),
        "later": frozenset({"writer"}),
    }


@pytest.mark.feature("swarm-tick-declared-edges")
def test_the_values_a_site_shut_check_reads_order_its_steps_after_their_writer() -> None:
    stopper = Step("stopper", "a", writes=frozenset({FINISHED}))
    shut = Step("shut", "b")
    open_site = Step("open", "c")
    edged = registry.site_edges((stopper, shut, open_site), ("a", "b", "c"), shuts={"b": frozenset({FINISHED})})
    assert _needs(edged) == {"stopper": frozenset(), "shut": frozenset({"stopper"}), "open": frozenset()}


@pytest.mark.feature("swarm-tick-declared-edges")
def test_a_slow_lane_writer_is_no_edge_of_a_later_site() -> None:
    slow = Step("slow", "a", writes=frozenset({ROWS}), lane=engine.SLOW)
    reader = Step("reader", "b", reads=frozenset({ROWS}))
    assert _needs(registry.site_edges((slow, reader), ("a", "b"))) == {"slow": frozenset(), "reader": frozenset()}


@pytest.mark.feature("swarm-tick-declared-edges")
def test_the_pass_graph_holds_no_step_on_an_earlier_step_whose_values_it_does_not_use() -> None:
    needs = _pass_needs()
    assert not needs["quota notice"] & {"dispatcher group", "size pass", "grouping release", "run window"}
    assert not needs["merge pass"] & {"settle", "close space", "rank pass", "quota notice"}
    assert "page relay" in needs["tick view"] and "intent check" in needs["page relay"]
    assert "ledger health" in needs["command runner"] and "stopped branch" in needs["quota notice"]


@pytest.mark.feature("swarm-tick-declared-edges")
def test_the_gate_runs_the_hooks_of_earlier_sites_before_a_later_step() -> None:
    ran: list[str] = []
    hooks = {site: (lambda site=site: ran.append(site) or True) for site in ("a", "b")}
    gate = engine.SiteGate(hooks, lambda step: False, sites=("a", "b", "c"))
    assert gate(Step("c1", "c"), {}) is True
    assert ran == ["a", "b"]
    assert gate(Step("a1", "a"), {}) is True
    assert ran == ["a", "b"]


@pytest.mark.feature("swarm-tick-declared-edges")
def test_a_shut_step_runs_no_hook_of_an_earlier_site() -> None:
    ran: list[str] = []
    gate = engine.SiteGate({"a": lambda: ran.append("a") or True}, lambda step: True, sites=("a", "b"))
    assert gate(Step("b1", "b"), {}) is False
    assert ran == []


@pytest.mark.feature("swarm-tick-declared-edges")
def test_a_left_value_shuts_only_the_sites_from_the_hook_that_found_it() -> None:
    sites = TickSites({}, {}, {}, lambda: 0)
    assert sites._listed() is False
    assert not sites.shut(Step("observer", "observe"))
    assert sites.shut(Step("reaper", "agents"))
    assert sites.shut(Step("closer", TICK_SITES[-1]))


@pytest.mark.feature("swarm-tick-declared-edges")
def test_a_later_hook_that_finds_its_value_keeps_the_earlier_left_site() -> None:
    sites = TickSites({}, {CONFIG: {}}, {}, lambda: 0)
    assert sites._listed() is False
    assert sites._configured() is True
    assert sites.left_from == "agents"
    assert sites.shut(Step("reaper", "agents"))
    assert sites.shut(Step("closer", TICK_SITES[-1]))
