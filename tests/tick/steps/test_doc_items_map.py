"""The doc items map tick step maps a changed watchdog document copy onto its tasks once per change."""

from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import doc_items
from scripts.tick import registry
from scripts.tick.adapters.fakes import FakeCoordination, FakeLedger, fake_tools
from scripts.tick.deadline import Deadline
from scripts.tick.steps import CallStep, pass_domains

pytestmark = pytest.mark.unit

COPY = """# Open work
1. [open] First item. [Ledger: t1]
2. [closed] Finished item. [Ledger: t2]
3. [open] Range item. [Ledger: t1 to t2]
"""


def _step() -> CallStep:
    import_module("scripts.tick.steps.report")
    found = {step.name: step for step in registry.steps()}["doc items map"]
    assert isinstance(found, CallStep)
    return found


def _values(copy: str | None, tasks: list[dict[str, Any]], marked: str | None = None) -> dict[str, Any]:
    tools = fake_tools()
    assert isinstance(tools.coordination, FakeCoordination)
    assert isinstance(tools.ledger, FakeLedger)
    tools.coordination.answers["marked"] = marked
    tools.ledger.answers["watchdog_copy"] = {} if copy is None else {"text": copy, "digest": doc_items.digest(copy)}
    return {
        "doc": {"tasks": tasks},
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


def _writes(values: dict[str, Any]) -> list[tuple[str, tuple[object, ...]]]:
    calls = [(call.operation, call.args) for call in _ledger(values).calls]
    assert calls[0] == ("watchdog_copy", ())
    return calls[1:]


def _coordination(values: dict[str, Any]) -> list[tuple[str, tuple[object, ...]]]:
    coordination = values["fakes"].coordination
    assert isinstance(coordination, FakeCoordination)
    return [(call.operation, call.args) for call in coordination.calls]


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
    assert found.reads == pass_domains("doc")
    assert found.needs == frozenset({"ledger state", "drained resume"})


@pytest.mark.feature("tick-doc-items-map")
def test_a_new_copy_on_the_ledger_maps_its_items_to_their_tasks_exactly_once() -> None:
    values = _values(COPY, _tasks())
    assert _step().call(None, values.__getitem__) == [
        "mapped watchdog doc items of the ledger copy: 3 items on 2 tasks, updated t1, t2"
    ]
    assert _writes(values) == [
        ("update_task", ("t1", {"doc_items": ["3 Range item.", "1 First item."]}, (), None)),
        ("update_task", ("t2", {"doc_items": ["3 Range item."]}, (), None)),
    ]
    assert _coordination(values) == [
        ("marked", (doc_items.MARK,)),
        ("mark", (doc_items.MARK, doc_items.digest(COPY))),
    ]
    recorded = _coordination(values)[-1][1][1]
    assert isinstance(recorded, str)
    again = _values(COPY, _tasks(), recorded)
    assert _step().call(None, again.__getitem__) == []
    assert _writes(again) == []
    assert _coordination(again) == [("marked", (doc_items.MARK,))]
    newer = COPY + "4. [open] New item. [Ledger: t2]\n"
    changed = _values(newer, _tasks(), recorded)
    assert _step().call(None, changed.__getitem__) == [
        "mapped watchdog doc items of the ledger copy: 4 items on 2 tasks, updated t1, t2"
    ]
    assert [args for _, args in _writes(changed)] == [
        ("t1", {"doc_items": ["3 Range item.", "1 First item."]}, (), None),
        ("t2", {"doc_items": ["3 Range item.", "4 New item."]}, (), None),
    ]
    assert _coordination(changed)[-1] == ("mark", (doc_items.MARK, doc_items.digest(newer)))


@pytest.mark.feature("tick-doc-items-map")
def test_an_unchanged_copy_runs_nothing() -> None:
    values = _values(COPY, _tasks(), doc_items.digest(COPY))
    assert _step().call(None, values.__getitem__) == []
    assert _writes(values) == []
    assert _coordination(values) == [("marked", (doc_items.MARK,))]


@pytest.mark.feature("tick-doc-items-map")
@pytest.mark.parametrize("copy", [None, ""])
def test_a_ledger_without_a_copy_runs_nothing(copy: str | None) -> None:
    values = _values(copy, _tasks())
    assert _step().call(None, values.__getitem__) == []
    assert _writes(values) == []
    assert _coordination(values) == []


@pytest.mark.feature("tick-doc-items-map")
def test_a_copy_already_on_its_tasks_records_its_digest_without_writes() -> None:
    tasks = [
        {"id": "t1", "doc_items": ["1 First item.", "3 Range item."]},
        {"id": "t2", "doc_items": ["3 Range item."]},
    ]
    values = _values(COPY, tasks)
    assert _step().call(None, values.__getitem__) == [
        "mapped watchdog doc items of the ledger copy: 3 items on 2 tasks, updated none"
    ]
    assert _writes(values) == []
    assert _coordination(values)[-1] == ("mark", (doc_items.MARK, doc_items.digest(COPY)))


@pytest.mark.feature("tick-doc-items-map")
def test_unknown_tasks_refuse_the_copy_once_until_it_changes() -> None:
    copy = COPY + "4. [open] Lost item. [Ledger: t9, t8]\n"
    values = _values(copy, _tasks())
    assert _step().call(None, values.__getitem__) == [
        "refused watchdog doc items of the ledger copy: Unknown ledger tasks: t8, t9"
    ]
    assert _writes(values) == []
    assert _coordination(values)[-1] == ("mark", (doc_items.MARK, doc_items.digest(copy)))


@pytest.mark.feature("tick-doc-items-map")
def test_an_invalid_ledger_tag_refuses_the_copy_once_until_it_changes() -> None:
    copy = COPY + "4. [open] Bad tag. [Ledger: nine]\n"
    values = _values(copy, _tasks())
    assert _step().call(None, values.__getitem__) == [
        "refused watchdog doc items of the ledger copy: Invalid Ledger tag:  nine"
    ]
    assert _writes(values) == []
    assert _coordination(values)[-1] == ("mark", (doc_items.MARK, doc_items.digest(copy)))


@pytest.mark.feature("tick-doc-items-map")
def test_the_step_stops_when_its_deadline_expires() -> None:
    values = _values(COPY, _tasks())
    values["deadline"] = Deadline.after(-1)
    with pytest.raises(TimeoutError, match="tool deadline expired"):
        _step().call(None, values.__getitem__)


@pytest.mark.feature("tick-doc-items-map")
def test_the_coordination_store_reads_back_the_mark_it_wrote() -> None:
    from scripts.tick.adapters.coordination import StoreCoordination

    class _Redis:
        def __init__(self) -> None:
            self.values: dict[str, str] = {}

        def set(self, key: str, value: str) -> bool:
            self.values[key] = value
            return True

        def get(self, key: str) -> str | None:
            return self.values.get(key)

    store = SimpleNamespace(redis=_Redis(), key=lambda slug, *parts: ":".join(("swarm", slug, *parts)))
    port = StoreCoordination(store, "sw", "owner")  # pyright: ignore[reportArgumentType]
    deadline = Deadline.after(5)
    assert port.marked(doc_items.MARK, deadline=deadline) is None
    assert port.mark(doc_items.MARK, "abc", deadline=deadline) is True
    assert port.marked(doc_items.MARK, deadline=deadline) == "abc"
    assert store.redis.values == {"swarm:sw:doc-items-digest": "abc"}


@pytest.mark.feature("tick-doc-items-map")
def test_the_swarm_setting_names_the_watchdog_document() -> None:
    from unittest.mock import Mock

    from scripts.swarm.cli import CONFIG_KEYS, config_value
    from scripts.swarm.store import RedisStore

    assert CONFIG_KEYS["watchdog-document"] == "watchdog_document"
    assert config_value("watchdog-document", "/copy/watchdog-doc.md") == "/copy/watchdog-doc.md"
    redis = Mock()
    raw = {"slug": "sw", "repo": "/repo", "max_eng": "1", "max_ci": "1", "state": "paused"}
    redis.hgetall.return_value = raw
    store = RedisStore(redis)
    assert store.config("sw").watchdog_document == ""
    raw["watchdog_document"] = "/copy/watchdog-doc.md"
    assert store.config("sw").watchdog_document == "/copy/watchdog-doc.md"
