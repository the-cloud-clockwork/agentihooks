"""The health checks that keep state between tick passes store it as one JSON text under their own key of the swarm:
the time of the pass first, their rows after it."""

import importlib
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from scripts.swarm.health import focus_unclaimed, kept, order_waiting
from scripts.swarm.store import SwarmConfig
from tests.swarm.test_freeze import doc, record, task
from tests.swarm.test_order_check import ORDER, ledger_doc
from tests.tick.steps.test_focus_unclaimed import store

SLUG = "sw"
START = 1_800_000_000_000
ROOT = Path(__file__).resolve().parents[3]
HEALTH = ROOT / "scripts" / "swarm" / "health"
RULE = ROOT / ".semgrep" / "features" / "health-kept-state-one-helper.yml"
BEFORE_THE_HELPER = frozenset(
    {
        "early_ends",
        "merge_stall",
        "night_watch",
        "ordering_dependencies",
        "panic_call",
        "prompt_backlog",
        "spawn_stall",
        "stall_blockers",
        "step_starvation",
        "urgent_hold",
    }
)
_fixtures = (store,)


def stored(found: Any, key: str) -> str | None:
    text: str | None = found.redis.get(found.key(SLUG, key))
    return text


@pytest.mark.feature("health-kept-state-one-helper")
def test_the_focus_check_keeps_its_counts_as_one_text_under_its_key(store: Any) -> None:
    assert stored(store, "focus-unclaimed") is None
    assert focus_unclaimed.read(store, SLUG) == {}
    ledger = doc(record("phases/p1", "focus"), tasks=[task("t1", title="Ship the fix")])
    focus_unclaimed.observe(store, SLUG, ledger, 1000)
    focus_unclaimed.observe(store, SLUG, ledger, 1001)
    assert stored(store, "focus-unclaimed") == (
        '{"at": 1001, "tasks": {"t1": {"ticks": 2, "since": 1000, "title": "Ship the fix", "focus": "phases/p1"}}}'
    )
    focus_unclaimed.observe(store, SLUG, doc(tasks=[task("t1")]), 1002)
    assert stored(store, "focus-unclaimed") == '{"at": 1002, "tasks": {}}'
    assert focus_unclaimed.read(store, SLUG) == {"at": 1002, "tasks": {}}


@pytest.mark.feature("health-kept-state-one-helper")
def test_the_order_check_keeps_its_waits_as_one_text_under_its_key(store: Any) -> None:
    assert stored(store, "order-waiting") is None
    assert order_waiting.read(store, SLUG) == {}
    ledger = ledger_doc([task("t1"), task("t2", depends_on=["t9"]), task("t9", phase="p2")], p1=ORDER)
    order_waiting.observe(store, SLUG, ledger, START)
    order_waiting.observe(store, SLUG, ledger, START + 60_000)
    assert stored(store, "order-waiting") == (
        '{"at": 1800000060000, "orders": {"phases/p1": {"since": 1800000000000, "title": "Build", '
        '"unfinished": 2, "claimable": 1, "reasons": ["it waits on task t9"]}}}'
    )
    order_waiting.observe(store, SLUG, ledger_doc([task("t1")]), START + 120_000)
    assert stored(store, "order-waiting") == '{"at": 1800000120000, "orders": {}}'
    assert order_waiting.read(store, SLUG) == {"at": START + 120_000, "orders": {}}


@pytest.mark.feature("health-kept-state-one-helper")
def test_each_check_keeps_its_state_apart_from_the_other_and_from_another_swarm(store: Any) -> None:
    store.create(SwarmConfig("other", "/repo", max_eng=2, max_ci=1))
    focus_unclaimed.observe(store, SLUG, doc(tasks=[task("t1")]), 7)
    assert (stored(store, "focus-unclaimed"), stored(store, "order-waiting")) == ('{"at": 7, "tasks": {}}', None)
    assert focus_unclaimed.read(store, "other") == order_waiting.read(store, "other") == {}


@pytest.mark.feature("health-kept-state-one-helper")
def test_the_helper_reads_empty_then_what_it_wrote_under_the_key_of_that_swarm(store: Any) -> None:
    assert kept.read(store, SLUG, "any-check") == {}
    kept.write(store, SLUG, "any-check", 9, "rows", {"a": [1, 2]})
    assert stored(store, "any-check") == '{"at": 9, "rows": {"a": [1, 2]}}'
    assert kept.read(store, SLUG, "any-check") == {"at": 9, "rows": {"a": [1, 2]}}
    assert kept.read(store, SLUG, "other-check") == {}
    kept.write(store, SLUG, "any-check", 10, "seen", [])
    assert stored(store, "any-check") == '{"at": 10, "seen": []}'


@pytest.mark.feature("health-kept-state-one-helper")
def test_the_helper_refuses_rows_kept_under_the_name_of_the_time_of_the_pass(store: Any) -> None:
    with pytest.raises(ValueError) as refused:
        kept.write(store, SLUG, "any-check", 9, "at", {"a": 1})
    assert str(refused.value) == "a check keeps its rows under a name other than at, the time of its pass"
    assert stored(store, "any-check") is None


@pytest.mark.feature("health-kept-state-one-helper")
def test_the_rule_excuses_only_checks_that_still_keep_state_on_their_own_and_did_before_the_helper() -> None:
    rule: dict[str, Any] = yaml.safe_load(RULE.read_text())["rules"][0]
    excused = {Path(path).stem for path in rule["paths"]["exclude"]} - {"kept"}
    assert excused <= BEFORE_THE_HELPER
    own = re.compile(r"redis\.(?:get|set)\(")
    assert [name for name in sorted(excused) if not own.search((HEALTH / f"{name}.py").read_text())] == []


def keepers() -> list[ModuleType]:
    """Every health check that keeps state through the helper, found in the tree."""
    uses = re.compile(r"\bkept\.(?:read|write)\(|from scripts\.swarm\.health\.kept import")
    found = sorted(path.stem for path in HEALTH.glob("*.py") if path.stem != "kept" and uses.search(path.read_text()))
    return [importlib.import_module(f"scripts.swarm.health.{name}") for name in found]


def named(check: ModuleType) -> str:
    return check.__name__.rpartition(".")[2]


@pytest.mark.feature("health-kept-key-own-module")
def test_the_checks_that_keep_state_through_the_helper_are_found_in_the_tree() -> None:
    assert {"focus_unclaimed", "order_waiting"} <= {named(check) for check in keepers()}


@pytest.mark.feature("health-kept-key-own-module")
@pytest.mark.parametrize("check", keepers(), ids=named)
def test_only_the_check_names_the_key_of_the_state_it_keeps(check: ModuleType) -> None:
    own = Path(str(check.__file__)).resolve()
    holders = [
        path.relative_to(ROOT).as_posix()
        for folder in ("scripts", "hooks")
        for path in sorted((ROOT / folder).rglob("*.py"))
        if path.resolve() != own and f'"{check.KEY}"' in path.read_text()
    ]
    assert holders == []
