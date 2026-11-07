"""The spawn due tick step holds its spawn while fleet panic is on and spawns once panic clears."""

from contextlib import contextmanager
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import tick
from scripts.tick.steps import observe

pytestmark = pytest.mark.unit

DUE = "sw:spawn-due"


class _Redis:
    def __init__(self) -> None:
        self.keys: dict[str, object] = {DUE: 1}

    def exists(self, key: str) -> bool:
        return key in self.keys

    def set(self, key: str, value: object) -> None:
        self.keys[key] = value


class _Store:
    def __init__(self) -> None:
        self.redis = _Redis()

    def key(self, slug: str, name: str) -> str:
        return f"{slug}:{name}"


@pytest.mark.feature("swarm-tick-panic-holds-spawn")
def test_spawn_due(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spawn due spawns nothing while panic holds, keeps the due mark, and spawns once panic clears."""
    spawned: list[str] = []
    panic: list[object] = ["halted"]

    @contextmanager
    def placement(runtime: object) -> Any:
        yield

    monkeypatch.setattr(tick, "_spawn", lambda *args: spawned.append(args[0]) or ["spawned"])
    monkeypatch.setattr(tick, "_placement_pass", placement)
    monkeypatch.setattr(tick.lifetime, "sleeping", lambda slug, store, rows: False)
    monkeypatch.setattr(tick.panic, "state", lambda store: panic[0])
    store = _Store()
    config: Any = SimpleNamespace(state="running", engine="tick")

    def step() -> list[str]:
        box: dict[str, Any] = {"rows": MappingProxyType({}), "doc": {"tasks": []}}
        return observe.step_spawn_due("sw", config, store, "ledger", "runtime", 7, box)

    assert step() == []
    assert spawned == []
    assert store.redis.exists(DUE)
    panic[0] = None
    assert step() == ["spawned"]
    assert spawned == ["sw"]
