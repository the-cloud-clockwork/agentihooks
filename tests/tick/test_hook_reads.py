"""Tick and wrapper site hooks read only the pass box keys they declare, checked at runtime: observe logs, enforce
raises."""

import logging
from typing import Any

import pytest

from scripts.swarm import cli
from scripts.tick import hook_reads, sites
from scripts.tick.hook_reads import ENV, UndeclaredHookRead, guarded
from scripts.tick.steps import Left, PassBox

pytestmark = pytest.mark.unit
LOGGER = "scripts.tick.hook_reads"


def _lines(caplog: pytest.LogCaptureFixture) -> list[tuple[str, int, str]]:
    return [(r.name, r.levelno, r.getMessage()) for r in caplog.records if r.name == LOGGER]


def _tick_sites(box: PassBox) -> sites.TickSites:
    return sites.TickSites({}, box, {}, lambda: 0)


def _wrapper(box: PassBox, judging: PassBox) -> Any:
    wrapper: Any = object.__new__(cli._Wrapper)
    wrapper.box = box
    wrapper.judging = judging
    return wrapper


@pytest.mark.feature("swarm-tick-hook-reads")
def test_an_undeclared_read_by_a_tick_site_hook_logs_in_observe_mode_and_the_hook_still_answers(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv(ENV, raising=False)
    box = PassBox({"config": 1, "doc": 2})
    monkeypatch.setattr(sites.TickSites, "_configured", lambda self: "doc" in self.box)
    caplog.set_level(logging.WARNING, logger=LOGGER)
    assert _tick_sites(box).hooks()["tasks"]() is True
    assert _lines(caplog) == [(LOGGER, logging.WARNING, "site hook tasks read pass box doc without declaring it")]


@pytest.mark.feature("swarm-tick-hook-reads")
def test_an_undeclared_read_by_a_wrapper_site_hook_raises_in_enforce_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, "enforce")
    monkeypatch.setattr(cli._Wrapper, "_count", lambda self: self.box["doc"] == 2)
    hooks = _wrapper(PassBox({"doc": 2}), PassBox()).hooks()
    with pytest.raises(UndeclaredHookRead) as raised:
        hooks["wrapper metrics"]()
    assert str(raised.value) == "site hook wrapper metrics read pass box doc without declaring it"


@pytest.mark.feature("swarm-tick-hook-reads")
def test_the_declared_hook_reads_pass_in_enforce_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, "ENFORCE ")
    box = PassBox({"config": 1, "doc": 2, "rows": 3})
    ticks = _tick_sites(box).hooks()
    assert ticks["agents"]() is True
    assert ticks["tasks"]() is True
    wrapper = _wrapper(PassBox({"agents": []}), PassBox())
    monkeypatch.setattr(cli._Wrapper, "_reread", lambda self: "agents" in self.box)
    monkeypatch.setattr(
        cli._Wrapper, "_judged", lambda self: "raised" not in self.judging and "judged" not in self.judging
    )
    hooks = wrapper.hooks()
    assert hooks["wrapper view"]() is True
    assert hooks["wrapper relay"]() is True


@pytest.mark.feature("swarm-tick-hook-reads")
def test_each_site_hook_declares_exactly_the_box_keys_it_reads() -> None:
    assert sites.TICK_HOOK_READS == {"agents": frozenset({"config", "doc", "rows"}), "tasks": frozenset({"config"})}
    assert cli.WRAPPER_HOOK_READS == {
        "wrapper relay": frozenset({"raised", "judged"}),
        "wrapper view": frozenset({"agents"}),
    }


@pytest.mark.feature("swarm-tick-hook-reads")
def test_off_mode_hands_back_the_hooks_unguarded(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    box = PassBox({"doc": 2})

    def hook() -> bool:
        return "doc" in box

    found = guarded({"s": hook}, {}, {ENV: "off"})
    assert found == {"s": hook}
    caplog.set_level(logging.WARNING, logger=LOGGER)
    assert found["s"]() is True
    assert _lines(caplog) == []


@pytest.mark.feature("swarm-tick-hook-reads")
def test_an_unknown_or_missing_mode_observes() -> None:
    assert hook_reads.mode({}) == "observe"
    assert hook_reads.mode({ENV: "deny"}) == "observe"
    assert hook_reads.mode({ENV: " Off"}) == "off"
    assert hook_reads.mode({ENV: "enforce"}) == "enforce"


@pytest.mark.feature("swarm-tick-hook-reads")
def test_reads_outside_a_hook_and_after_it_ends_are_not_checked(caplog: pytest.LogCaptureFixture) -> None:
    box = PassBox({"doc": 2})

    def failing() -> bool:
        raise ValueError("hook failed")

    found = guarded({"s": lambda: box.get("doc") == 2, "f": failing}, {}, {ENV: "enforce"})
    with pytest.raises(UndeclaredHookRead):
        found["s"]()
    with pytest.raises(ValueError):
        found["f"]()
    caplog.set_level(logging.WARNING, logger=LOGGER)
    assert box["doc"] == 2
    assert box.get("rows", 5) == 5
    assert "doc" in box
    assert hook_reads.HOOK.get() is None
    assert _lines(caplog) == []


@pytest.mark.feature("swarm-tick-hook-reads")
def test_every_pass_box_read_kind_reaches_the_guard_and_keeps_its_answer() -> None:
    box = PassBox({"doc": 2})
    found = guarded(
        {
            "item": lambda: box["doc"] == 2,
            "get": lambda: box.get("rows", 5) == 5,
            "contains": lambda: "view" in box,
        },
        {},
        {ENV: "enforce"},
    )
    for site, key in (("item", "doc"), ("get", "rows"), ("contains", "view")):
        with pytest.raises(UndeclaredHookRead) as raised:
            found[site]()
        assert str(raised.value) == f"site hook {site} read pass box {key} without declaring it"
    allowed = guarded(
        {"get": lambda: box.get("rows", 5) == 5 and box.get("doc") == 2}, {"get": frozenset({"rows", "doc"})}
    )
    assert allowed["get"]() is True
    with pytest.raises(Left):
        box["missing"]
