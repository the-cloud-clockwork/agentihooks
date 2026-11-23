"""The health checks that keep state between tick passes store it as one JSON text under their own key of the swarm:
the time of the pass first, their rows after it."""

from typing import Any

import pytest

from scripts.swarm.health import focus_unclaimed, order_waiting
from tests.swarm.test_freeze import doc, record, task
from tests.swarm.test_order_check import ORDER, ledger_doc
from tests.tick.steps.test_focus_unclaimed import store

SLUG = "sw"
START = 1_800_000_000_000
_fixtures = (store,)


def kept(found: Any, key: str) -> str | None:
    text: str | None = found.redis.get(found.key(SLUG, key))
    return text


@pytest.mark.feature("health-kept-state-one-helper")
def test_the_focus_check_keeps_its_counts_as_one_text_under_its_key(store: Any) -> None:
    assert kept(store, "focus-unclaimed") is None
    assert focus_unclaimed.read(store, SLUG) == {}
    ledger = doc(record("phases/p1", "focus"), tasks=[task("t1", title="Ship the fix")])
    focus_unclaimed.observe(store, SLUG, ledger, 1000)
    focus_unclaimed.observe(store, SLUG, ledger, 1001)
    assert kept(store, "focus-unclaimed") == (
        '{"at": 1001, "tasks": {"t1": {"ticks": 2, "since": 1000, "title": "Ship the fix", "focus": "phases/p1"}}}'
    )
    focus_unclaimed.observe(store, SLUG, doc(tasks=[task("t1")]), 1002)
    assert kept(store, "focus-unclaimed") == '{"at": 1002, "tasks": {}}'
    assert focus_unclaimed.read(store, SLUG) == {"at": 1002, "tasks": {}}


@pytest.mark.feature("health-kept-state-one-helper")
def test_the_order_check_keeps_its_waits_as_one_text_under_its_key(store: Any) -> None:
    assert kept(store, "order-waiting") is None
    assert order_waiting.read(store, SLUG) == {}
    ledger = ledger_doc([task("t1"), task("t2", depends_on=["t9"]), task("t9", phase="p2")], p1=ORDER)
    order_waiting.observe(store, SLUG, ledger, START)
    order_waiting.observe(store, SLUG, ledger, START + 60_000)
    assert kept(store, "order-waiting") == (
        '{"at": 1800000060000, "orders": {"phases/p1": {"since": 1800000000000, "title": "Build", '
        '"unfinished": 2, "claimable": 1, "reasons": ["it waits on task t9"]}}}'
    )
    order_waiting.observe(store, SLUG, ledger_doc([task("t1")]), START + 120_000)
    assert kept(store, "order-waiting") == '{"at": 1800000120000, "orders": {}}'
    assert order_waiting.read(store, SLUG) == {"at": START + 120_000, "orders": {}}


@pytest.mark.feature("health-kept-state-one-helper")
def test_each_check_keeps_its_state_apart_from_the_other_and_from_another_swarm(store: Any) -> None:
    from scripts.swarm.store import SwarmConfig

    store.create(SwarmConfig("other", "/repo", max_eng=2, max_ci=1))
    focus_unclaimed.observe(store, SLUG, doc(tasks=[task("t1")]), 7)
    assert (kept(store, "focus-unclaimed"), kept(store, "order-waiting")) == ('{"at": 7, "tasks": {}}', None)
    assert focus_unclaimed.read(store, "other") == order_waiting.read(store, "other") == {}
