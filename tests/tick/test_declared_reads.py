"""The tick step reader refuses every pass box value a step does not declare, and every site hook declares its keys."""

from types import SimpleNamespace
from typing import Any, cast

import pytest

from scripts.swarm import cli
from scripts.tick import registry, sites
from scripts.tick import steps as tick_steps
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain
from scripts.tick.steps import CallStep, DeclaredBox, Left, PassBox, UndeclaredRead, hook_domains, pass_domains, tasks

HOOK_SITES = (*cli.PASS_SITES, "closing")


class _View:
    def __init__(self, state: dict[str, Any], box: PassBox) -> None:
        self.state = state
        self.box = box

    def read(self, domain: Domain, key: str) -> object | None:
        if key == "box":
            return self.box
        return self.box[key] if key in self.box else self.state.get(key)


def _ctx(box: PassBox) -> Context:
    return Context(_View({"slug": "sw"}, box), Deadline.after(60.0), None)


def _reading(key: str) -> Any:
    def call(_tick: Any, get: Any) -> list[str]:
        return [f"{key}={get(key)}"]

    return call


@pytest.mark.feature("swarm-tick-declared-reads")
def test_a_step_reading_a_box_value_it_does_not_declare_is_refused() -> None:
    step = CallStep("verify", "observe", _reading("rows"))
    with pytest.raises(UndeclaredRead, match="step verify reads pass value rows it does not declare"):
        step.run(_ctx(PassBox({"rows": {"t1": {}}})))


@pytest.mark.feature("swarm-tick-declared-reads")
def test_a_step_reading_a_box_value_no_step_stored_yet_is_refused_by_name() -> None:
    step = CallStep("verify", "observe", _reading("doc"))
    with pytest.raises(UndeclaredRead, match="pass value doc"):
        step.run(_ctx(PassBox()))


@pytest.mark.feature("swarm-tick-declared-reads")
def test_declared_step_and_hook_values_and_pass_state_read_through() -> None:
    box = PassBox({"rows": 1, "doc": 2})
    rows = CallStep("verify", "observe", _reading("rows"), reads=pass_domains("rows"))
    doc = CallStep("track outages", "observe", _reading("doc"), reads=hook_domains("doc"))
    slug = CallStep("launch checks", "observe", _reading("slug"))
    found = [step.run(_ctx(box)).writes[0].value for step in (rows, doc, slug)]
    assert found == [["rows=1"], ["doc=2"], ["slug=sw"]]


@pytest.mark.feature("swarm-tick-declared-reads")
def test_the_box_view_refuses_undeclared_keys_and_writes_through() -> None:
    box = PassBox({"rows": 1, "raised": None})
    view = DeclaredBox(box, "viewer", tick_steps.declared_keys(pass_domains("doc")))
    with pytest.raises(UndeclaredRead, match="step viewer reads pass value rows"):
        _ = view["rows"]
    with pytest.raises(UndeclaredRead):
        _ = "rows" in view
    with pytest.raises(Left):
        _ = view["doc"]
    assert "raised" in view
    assert view.get("doc", "none") == "none"
    view["doc"] = 3
    assert box["doc"] == 3
    assert dict(view) == {"raised": None, "doc": 3}
    assert len(view) == 2
    assert "scratch" not in view
    box["scratch"] = 4
    with pytest.raises(UndeclaredRead, match="pass value scratch"):
        _ = "scratch" in view
    with pytest.raises(Left):
        _ = DeclaredBox({}, "viewer", frozenset({"doc"}))["doc"]


@pytest.mark.feature("swarm-tick-declared-reads")
def test_a_pass_box_in_the_pass_state_is_guarded_and_a_plain_state_dict_is_not() -> None:
    scheduled = {"job": 1}
    state = {"slug": "sw", "judging": PassBox({"doc": 1}), "scheduled": scheduled}

    def judged(_tick: Any, get: Any) -> list[object]:
        return [get("judging")["doc"], get("scheduled") is scheduled, isinstance(get("box"), DeclaredBox)]

    ctx = Context(_View(state, cast(PassBox, {})), Deadline.after(60.0), None)
    with pytest.raises(UndeclaredRead, match="step verify reads pass value doc"):
        CallStep("verify", "observe", judged).run(ctx)
    found = CallStep("verify", "observe", judged, reads=hook_domains("doc")).run(ctx)
    assert found.writes[0].value == [1, True, True]


@pytest.mark.feature("swarm-tick-declared-reads")
def test_a_write_step_reads_what_its_reads_and_writes_declare() -> None:
    box = PassBox({"doc": 1, "phase changed": True})

    def call(_tick: Any, get: Any) -> list[str]:
        return [f"{get('doc')} {get('phase changed')}"]

    name = "planning and phase pass"
    declared = tasks.WriteStep(name, "g", call, reads=hook_domains("doc"), writes=pass_domains("phase changed"))
    assert declared.run(_ctx(box)).writes[0].value == ["1 True"]
    with pytest.raises(UndeclaredRead, match=f"step {name} reads pass value doc it does not declare"):
        tasks.WriteStep(name, "g", call, writes=pass_domains("phase changed")).run(_ctx(box))
    with pytest.raises(UndeclaredRead, match=f"step {name} reads pass value phase changed it does not declare"):
        tasks.WriteStep(name, "g", call, reads=hook_domains("doc")).run(_ctx(box))


@pytest.mark.feature("swarm-tick-declared-reads")
def test_every_site_hook_declares_its_keys() -> None:
    wrapper = cli._Wrapper.hooks(cast(Any, SimpleNamespace(**{name: name for name in _wrapper_hooks()})))
    ticks = sites.TickSites.hooks(cast(Any, SimpleNamespace(_start=1, _listed=2, _configured=3)))
    assert set(wrapper) == set(cli.WRAPPER_HOOK_KEYS)
    assert set(ticks) == set(sites.TICK_HOOK_KEYS)


@pytest.mark.feature("swarm-tick-declared-reads")
def test_every_hook_value_a_step_reads_is_written_by_a_hook_at_or_before_its_site() -> None:
    keys = {**cli.WRAPPER_HOOK_KEYS, **sites.TICK_HOOK_KEYS}
    order = {site: index for index, site in enumerate(HOOK_SITES)}
    written = [(order[site], writes) for site, (_, writes) in keys.items()]
    for step in registry.steps():
        for domain in step.reads:
            if domain.startswith(tick_steps.HOOK_PREFIX):
                at = order[step.group]
                assert any(index <= at and domain in writes for index, writes in written), (step.name, domain)


@pytest.mark.feature("swarm-tick-declared-reads")
def test_the_box_keys_are_the_values_steps_and_hooks_declare_writing() -> None:
    keys = {**cli.WRAPPER_HOOK_KEYS, **sites.TICK_HOOK_KEYS}
    domains = {domain for step in registry.steps() for domain in step.writes}
    domains |= {domain for _, writes in keys.values() for domain in writes}
    named = tick_steps.declared_keys(domains) - {tick_steps.RAISED}
    reads = {domain for step in registry.steps() for domain in step.reads}
    reads |= {domain for read, _ in keys.values() for domain in read}
    hooked = {domain for domain in reads if domain.startswith(tick_steps.HOOK_PREFIX)}
    assert named == tick_steps.BOX_KEYS
    assert tick_steps.declared_keys(hooked) - {tick_steps.RAISED} <= tick_steps.BOX_KEYS


def _wrapper_hooks() -> tuple[str, ...]:
    return ("_fence", "_watch", "_read", "_judge", "_judged", "_reread", "_count")
