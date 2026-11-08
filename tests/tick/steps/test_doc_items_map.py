"""The doc items map tick step maps a changed watchdog document copy onto its tasks once per change."""

from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import doc_items
from scripts.tick import registry
from scripts.tick.adapters.fakes import FakeCoordination, FakeLedger, fake_tools
from scripts.tick.deadline import Deadline
from scripts.tick.steps import CallStep, pass_domains

pytestmark = pytest.mark.unit

SLUG = "sw"
COPY = """# Open work
1. [open] First item. [Ledger: t1]
2. [closed] Finished item. [Ledger: t2]
3. [open] Range item. [Ledger: t1 to t2]
"""


class _Redis:
    """Answer the stored swarm marks by key."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)


class _Store:
    """Name swarm keys the way the swarm store does and hold their values."""

    def __init__(self) -> None:
        self.redis = _Redis()

    def key(self, slug: str, *parts: str) -> str:
        return ":".join(("swarm", slug, *parts))


def _step() -> CallStep:
    import_module("scripts.tick.steps.report")
    found = {step.name: step for step in registry.steps()}["doc items map"]
    assert isinstance(found, CallStep)
    return found


def _values(document: str, tasks: list[dict[str, Any]], store: _Store) -> dict[str, Any]:
    tools = fake_tools()
    return {
        "config": SimpleNamespace(watchdog_document=document),
        "doc": {"tasks": tasks},
        "store": store,
        "slug": SLUG,
        "tools": tools.for_effects(frozenset({"ledger", "coordination"})),
        "deadline": Deadline.after(5),
        "fakes": tools,
    }


def _tasks() -> list[dict[str, Any]]:
    return [{"id": "t1", "doc_items": ["3 Range item."]}, {"id": "t2"}]


def _ledger(values: dict[str, Any]) -> FakeLedger:
    ledger = values["fakes"].ledger
    assert isinstance(ledger, FakeLedger)
    return ledger


def _coordination(values: dict[str, Any]) -> FakeCoordination:
    coordination = values["fakes"].coordination
    assert isinstance(coordination, FakeCoordination)
    return coordination


def _remember(values: dict[str, Any], store: _Store) -> None:
    for call in _coordination(values).calls:
        name, value = call.args
        store.redis.values[store.key(SLUG, str(name))] = str(value)


@pytest.mark.feature("tick-doc-items-map")
def test_doc_items_map() -> None:
    """The doc items map step registers in group wrapper events on the slow lane with its feature and ports."""
    found = _step()
    assert (found.group, found.lane, found.feature, found.effects) == (
        "wrapper events",
        "slow",
        "tick-doc-items-map",
        frozenset({"coordination", "ledger"}),
    )
    assert found.reads == pass_domains("config", "doc")
    assert found.needs == frozenset({"ledger state", "drained resume"})


@pytest.mark.feature("tick-doc-items-map")
def test_a_changed_copy_maps_its_new_items_to_their_tasks_exactly_once(tmp_path: Path) -> None:
    copy = tmp_path / "watchdog-doc.md"
    copy.write_text(COPY)
    store = _Store()
    values = _values(str(copy), _tasks(), store)
    assert _step().call(None, values.__getitem__) == [
        f"mapped watchdog doc items of {copy}: 3 items on 2 tasks, updated t1, t2"
    ]
    assert [(call.operation, call.args) for call in _ledger(values).calls] == [
        ("update_task", ("t1", {"doc_items": ["3 Range item.", "1 First item."]})),
        ("update_task", ("t2", {"doc_items": ["3 Range item."]})),
    ]
    assert [(call.operation, call.args) for call in _coordination(values).calls] == [
        ("mark", (doc_items.MARK, doc_items.digest(COPY)))
    ]
    _remember(values, store)
    again = _values(str(copy), _tasks(), store)
    assert _step().call(None, again.__getitem__) == []
    assert _ledger(again).calls == []
    assert _coordination(again).calls == []
    copy.write_text(COPY + "4. [open] New item. [Ledger: t2]\n")
    changed = _values(str(copy), _tasks(), store)
    assert _step().call(None, changed.__getitem__) == [
        f"mapped watchdog doc items of {copy}: 4 items on 2 tasks, updated t1, t2"
    ]
    assert [call.args for call in _ledger(changed).calls] == [
        ("t1", {"doc_items": ["3 Range item.", "1 First item."]}),
        ("t2", {"doc_items": ["3 Range item.", "4 New item."]}),
    ]


@pytest.mark.feature("tick-doc-items-map")
def test_an_unchanged_copy_runs_nothing(tmp_path: Path) -> None:
    copy = tmp_path / "watchdog-doc.md"
    copy.write_text(COPY)
    store = _Store()
    store.redis.values[store.key(SLUG, doc_items.MARK)] = doc_items.digest(COPY)
    values = _values(str(copy), _tasks(), store)
    assert _step().call(None, values.__getitem__) == []
    assert _ledger(values).calls == []
    assert _coordination(values).calls == []


@pytest.mark.feature("tick-doc-items-map")
@pytest.mark.parametrize("document", ["", "missing.md"])
def test_an_unset_or_missing_copy_runs_nothing(tmp_path: Path, document: str) -> None:
    values = _values(str(tmp_path / document) if document else "", _tasks(), _Store())
    assert _step().call(None, values.__getitem__) == []
    assert _ledger(values).calls == []
    assert _coordination(values).calls == []


@pytest.mark.feature("tick-doc-items-map")
def test_a_copy_already_on_its_tasks_records_its_digest_without_writes(tmp_path: Path) -> None:
    copy = tmp_path / "watchdog-doc.md"
    copy.write_text(COPY)
    tasks = [
        {"id": "t1", "doc_items": ["1 First item.", "3 Range item."]},
        {"id": "t2", "doc_items": ["3 Range item."]},
    ]
    values = _values(str(copy), tasks, _Store())
    assert _step().call(None, values.__getitem__) == [
        f"mapped watchdog doc items of {copy}: 3 items on 2 tasks, updated none"
    ]
    assert _ledger(values).calls == []
    assert [call.args for call in _coordination(values).calls] == [(doc_items.MARK, doc_items.digest(COPY))]


@pytest.mark.feature("tick-doc-items-map")
def test_unknown_tasks_refuse_the_copy_once_until_it_changes(tmp_path: Path) -> None:
    copy = tmp_path / "watchdog-doc.md"
    copy.write_text(COPY + "4. [open] Lost item. [Ledger: t9, t8]\n")
    values = _values(str(copy), _tasks(), _Store())
    assert _step().call(None, values.__getitem__) == [
        f"refused watchdog doc items of {copy}: Unknown ledger tasks: t8, t9"
    ]
    assert _ledger(values).calls == []
    assert [call.args for call in _coordination(values).calls] == [(doc_items.MARK, doc_items.digest(copy.read_text()))]


@pytest.mark.feature("tick-doc-items-map")
def test_the_step_stops_when_its_deadline_expires(tmp_path: Path) -> None:
    copy = tmp_path / "watchdog-doc.md"
    copy.write_text(COPY)
    values = _values(str(copy), _tasks(), _Store())
    values["deadline"] = Deadline.after(-1)
    with pytest.raises(TimeoutError, match="tool deadline expired"):
        _step().call(None, values.__getitem__)


@pytest.mark.feature("tick-doc-items-map")
def test_the_swarm_setting_names_the_watchdog_document() -> None:
    from unittest.mock import Mock

    from scripts.swarm.cli import CONFIG_KEYS, config_value
    from scripts.swarm.store import RedisStore

    assert CONFIG_KEYS["watchdog-document"] == "watchdog_document"
    assert config_value("watchdog-document", "/copy/watchdog-doc.md") == "/copy/watchdog-doc.md"
    redis = Mock()
    raw = {"slug": SLUG, "repo": "/repo", "max_eng": "1", "max_ci": "1", "state": "paused"}
    redis.hgetall.return_value = raw
    store = RedisStore(redis)
    assert store.config(SLUG).watchdog_document == ""
    raw["watchdog_document"] = "/copy/watchdog-doc.md"
    assert store.config(SLUG).watchdog_document == "/copy/watchdog-doc.md"
