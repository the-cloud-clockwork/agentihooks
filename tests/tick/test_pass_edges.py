"""Every pass box value a tick step reads is declared, with a need on the step that wrote it, over the twelve scenarios."""

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

from scripts.tick import registry
from scripts.tick.step import Context, Domain, Result, Step
from scripts.tick.steps import PassBox, masters, pass_domain
from tests.swarm.tick_scenarios.test_characterisation import SCENARIOS, replay

tick_module = import_module("scripts.swarm.tick")
STEP: ContextVar[str | None] = ContextVar("pass edge step", default=None)
ENTRIES = ("tick", "run_tick")
HELD = "raised"
KEYS = frozenset(
    {"config", "finished", "doc", "rows", "sleeping", "agents", "view", "judged", "live", "found", "started", "stop"}
)
WRITTEN = frozenset({"config", "doc", "rows", "sleeping", "agents", "view", "judged", "live", "found", "started"})


@dataclass(frozen=True)
class Seen:
    step: str
    key: str
    writer: str | None


@dataclass
class Recorder:
    writes: list[tuple[int, str, object, str]] = field(default_factory=list)
    reads: set[Seen] = field(default_factory=set)
    written: set[tuple[str, str]] = field(default_factory=set)
    ran: set[str] = field(default_factory=set)

    def wrote(self, box: object, key: str, value: object) -> None:
        step = STEP.get()
        if step is not None and key != HELD:
            self.writes.append((id(box), key, value, step))
            self.written.add((step, key))

    def writer(self, key: str, value: object, box: object | None) -> str | None:
        return next(
            (
                step
                for held, written, found, step in reversed(self.writes)
                if written == key and found is value and (box is None or held == id(box))
            ),
            None,
        )

    def boxed(self, box: dict[str, Any], key: str) -> None:
        step = STEP.get()
        if step is not None and key in KEYS | {key for _, key in self.written}:
            present = dict.__contains__(box, key)
            value = dict.get(box, key) if present else None
            self.reads.add(Seen(step, key, self.writer(key, value, None) if present else None))

    def viewed(self, box: object, key: str, value: object) -> None:
        step = STEP.get()
        if step is not None and key in KEYS and (writer := self.writer(key, value, box)) is not None:
            self.reads.add(Seen(step, key, writer))


def named(run: Callable[[Context], Result], name: str, rec: Recorder) -> Callable[[Context], Result]:
    def recorded(ctx: Context) -> Result:
        rec.ran.add(name)
        token = STEP.set(name)
        try:
            return run(ctx)
        finally:
            STEP.reset(token)

    return recorded


def record(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    rec = Recorder()
    for step in registry.steps():
        monkeypatch.setattr(step, "run", named(step.run, step.name, rec))

    def setitem(box: dict[str, Any], key: str, value: object) -> None:
        rec.wrote(box, key, value)
        dict.__setitem__(box, key, value)

    def update(box: dict[str, Any], *args: Any, **values: Any) -> None:
        for key, value in dict(*args, **values).items():
            setitem(box, key, value)

    def getitem(box: dict[str, Any], key: str) -> Any:
        rec.boxed(box, key)
        return dict.__getitem__(box, key)

    def get(box: dict[str, Any], key: str, default: object = None) -> Any:
        rec.boxed(box, key)
        return dict.get(box, key, default)

    def contains(box: dict[str, Any], key: object) -> bool:
        if isinstance(key, str):
            rec.boxed(box, key)
        return dict.__contains__(box, key)

    for name, method in (
        ("__setitem__", setitem),
        ("update", update),
        ("__getitem__", getitem),
        ("get", get),
        ("__contains__", contains),
    ):
        monkeypatch.setattr(PassBox, name, method)
    pass_read = tick_module._PassView.read
    site_read = masters._SiteView.read

    def read_pass(view: Any, domain: Domain, key: str) -> object | None:
        value = pass_read(view, domain, key)
        rec.viewed(view._state.get("box"), key, value)
        return value

    def read_site(view: Any, domain: Domain, key: str) -> object | None:
        value = site_read(view, domain, key)
        rec.viewed(view._found.get("box"), key, value)
        return value

    monkeypatch.setattr(tick_module._PassView, "read", read_pass)
    monkeypatch.setattr(masters._SiteView, "read", read_site)
    return rec


def ancestors(steps: tuple[Step, ...]) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for wave in registry.waves():
        for step in wave:
            found[step.name] = set(step.needs).union(*(found[need] for need in step.needs))
    return {step.name: found[step.name] for step in steps}


def violations(rec: Recorder) -> list[str]:
    steps = registry.steps()
    by_name = {step.name: step for step in steps}
    before = ancestors(steps)
    out: list[str] = []
    for seen in sorted(rec.reads, key=lambda seen: (seen.step, seen.key, seen.writer or "")):
        step = by_name[seen.step]
        if pass_domain(seen.key) not in step.reads:
            out.append(f"{seen.step} reads pass {seen.key} without declaring it")
        declared = {other.name for other in steps if pass_domain(seen.key) in other.writes}
        writers = {seen.writer} if seen.writer else declared
        if not writers & (before[seen.step] | {seen.step}):
            out.append(f"{seen.step} reads pass {seen.key} without needing its writer {sorted(writers)}")
    for name, key in sorted(rec.written):
        if pass_domain(key) not in by_name[name].writes:
            out.append(f"{name} writes pass {key} without declaring it")
    return out


def replayed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, runs: tuple[tuple[str, str], ...]) -> Recorder:
    rec = record(monkeypatch)
    for scenario, entry in runs:
        replay(scenario, entry, monkeypatch, tmp_path / entry / scenario)
    return rec


@pytest.mark.feature("swarm-tick-pass-edges")
def test_every_pass_box_read_is_declared_with_a_need_on_its_writer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    rec = replayed(monkeypatch, tmp_path, tuple((scenario, entry) for scenario in SCENARIOS for entry in ENTRIES))
    broken = violations(rec)
    assert not broken, (
        f"{len(broken)} broken of {len(rec.reads)} pass box reads and {len(rec.written)} writes:\n" + "\n".join(broken)
    )
    assert {key for _, key in rec.written} >= WRITTEN
    assert {seen.key for seen in rec.reads} >= WRITTEN - {"started", "judged"}
    skipped = sorted({step.name for step in registry.steps()} - rec.ran)
    assert not skipped, f"{len(skipped)} registered steps never ran: {skipped}"


@pytest.mark.feature("swarm-tick-pass-edges")
def test_a_planted_undeclared_read_is_named(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    verify = next(step for step in registry.steps() if step.name == "verify")
    monkeypatch.setattr(verify, "reads", verify.reads - {pass_domain("rows")})
    rec = replayed(monkeypatch, tmp_path, (("running", "tick"),))
    assert "verify reads pass rows without declaring it" in violations(rec)


@pytest.mark.feature("swarm-tick-pass-edges")
def test_a_planted_missing_need_is_named() -> None:
    rec = Recorder()
    rec.reads.add(Seen("bottleneck status", "found", "health findings"))
    assert violations(rec) == [
        "bottleneck status reads pass found without declaring it",
        "bottleneck status reads pass found without needing its writer ['health findings']",
    ]
