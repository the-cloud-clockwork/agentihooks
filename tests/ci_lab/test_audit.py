from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from scripts.ci_lab import audit
from scripts.ci_lab.digests import digest
from scripts.ci_lab.objects import LocalObjects
from scripts.ci_lab.store import DISABLED, Store, Verdict
from scripts.ci_lab.units import Priority

if TYPE_CHECKING:
    import fakeredis

SLUG = "lab-ledger"
HEAD = "a" * 40
OTHER = "b" * 40
AUDIT_RUN = "grade-v1-audit"
DAY = "2026-11-18"


def _key(kind: str, name: str) -> str:
    return f"{kind}-v1-{digest({'seed': name})}"


LINT = _key("static", "lint")
DOCS = _key("static", "docs")
ONE = _key("test", "one")
TWO = _key("test", "two")
SAME = _key("test", "same")
FLAKY = _key("test", "flaky")
LOST = _key("test", "lost")
UNKNOWN = _key("test", "unknown")
ELSEWHERE = _key("test", "elsewhere")
RED = _key("mutant", "red")


@pytest.fixture
def client() -> fakeredis.FakeRedis:
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


@pytest.fixture
def store(client: fakeredis.FakeRedis, tmp_path: Path) -> Store:
    return Store(client, LocalObjects(tmp_path))


@dataclass
class Lab:
    """A lab that answers a head's suite and a day's hits from lists and grades from a table of fresh outcomes."""

    units: list[audit.Cached] = field(default_factory=list[audit.Cached])
    days: dict[str, list[audit.Cached]] = field(default_factory=dict[str, list[audit.Cached]])
    fresh: dict[str, bool] = field(default_factory=dict[str, bool])
    graded: list[tuple[list[audit.Cached], Priority]] = field(default_factory=list[tuple[list[audit.Cached], Priority]])
    run: str = AUDIT_RUN

    def suite(self, head: str) -> list[audit.Cached]:
        return [unit for unit in self.units if unit.head == head]

    def hits(self, day: str) -> list[audit.Cached]:
        return self.days.get(day, [])

    def grade(self, units: Sequence[audit.Cached], tier: Priority) -> audit.Regrade:
        self.graded.append((list(units), tier))
        verdicts = tuple(
            Verdict(unit.kind, unit.key, self.fresh[unit.key], False, 1.0) for unit in units if unit.key in self.fresh
        )
        return audit.Regrade(self.run, verdicts)


class LedgerDown(Exception):
    """The ledger refused a write."""


@dataclass
class Ledger:
    """A ledger that records every task it is asked to add, or refuses every write while down or once it holds takes."""

    added: list[tuple[str, dict[str, Any], str]] = field(default_factory=list[tuple[str, dict[str, Any], str]])
    down: bool = False
    takes: int | None = None

    def add_task(self, slug: str, fields: dict[str, Any], by: str) -> None:
        if self.down or (self.takes is not None and len(self.added) >= self.takes):
            raise LedgerDown(slug)
        self.added.append((slug, fields, by))


def _cache(store: Store, kind: str, key: str, passed: bool, *, flaky: bool = False) -> Verdict:
    verdict = Verdict(kind, key, passed, flaky, 2.0)
    store.put(verdict, {"seconds": 2.0}, b"log")
    return verdict


def _unit(kind: str, key: str, run: str, head: str = HEAD) -> audit.Cached:
    return audit.Cached(kind, key, head, run)


def _hits(count: int) -> list[audit.Cached]:
    return [_unit("test", _key("test", f"hit {n}"), f"grade-v1-{n:03d}") for n in range(count)]


@pytest.mark.feature("lab-reuse-audit")
def test_a_seeded_mismatch_disables_the_kind_files_a_ci_task_with_both_runs_and_raises(
    store: Store, client: fakeredis.FakeRedis
) -> None:
    cached = _cache(store, "static", LINT, True)
    _cache(store, "static", DOCS, True)
    _cache(store, "test", ONE, True)
    units = [
        _unit("static", LINT, "grade-v1-cached"),
        _unit("static", DOCS, "grade-v1-cached"),
        _unit("test", ONE, "grade-v1-older"),
        _unit("test", ELSEWHERE, "grade-v1-older", OTHER),
    ]
    lab = Lab(units=units, fresh={LINT: False, DOCS: True, ONE: True})
    ledger = Ledger()

    with pytest.raises(audit.CILabVerdictMismatch) as raised:
        audit.promotion_audit(audit.Ports(store, lab, ledger, SLUG), HEAD)

    fresh = Verdict("static", LINT, False, False, 1.0)
    assert raised.value.mismatches == (audit.Mismatch(cached, fresh, "grade-v1-cached", AUDIT_RUN),)
    assert str(raised.value) == "regraded verdicts differ from the cached ones for static"
    assert lab.graded == [(units[:3], Priority.PROMOTION)]
    assert client.smembers(DISABLED) == {"static"}
    assert store.get("static", DOCS) is None
    assert store.get("test", ONE) is not None
    task = f"lab-mismatch-static-{digest({'kind': 'static', 'run': AUDIT_RUN})[:16]}"
    description = (
        "The lab audit regraded static units differently from their cached verdicts: "
        f"{LINT} cached passed by run grade-v1-cached, regraded failed by run {AUDIT_RUN}. "
        "Reuse of static verdicts is off until a pull request enables the kind again "
        "with the fix for the input its key misses."
    )
    assert ledger.added == [
        (
            SLUG,
            {
                "task": task,
                "title": "Lab verdict mismatch: static reuse disabled",
                "lane": "ci",
                "kind": "ci",
                "rank": "urgent",
                "description": description,
                "not_duplicate": audit.NOT_DUPLICATE,
            },
            "swarm",
        )
    ]


@pytest.mark.feature("lab-reuse-audit")
def test_matching_verdicts_file_nothing_and_return_the_audit_run(store: Store, client: fakeredis.FakeRedis) -> None:
    _cache(store, "static", LINT, True)
    _cache(store, "test", ONE, False)
    units = [_unit("static", LINT, "grade-v1-cached"), _unit("test", ONE, "grade-v1-cached")]
    lab = Lab(units=units, fresh={LINT: True, ONE: False})
    ledger = Ledger()

    regrade = audit.promotion_audit(audit.Ports(store, lab, ledger, SLUG), HEAD)

    assert regrade.run == AUDIT_RUN
    assert len(regrade.verdicts) == 2
    assert ledger.added == []
    assert client.smembers(DISABLED) == set()


@pytest.mark.feature("lab-reuse-audit")
def test_a_cached_flaky_pass_that_fails_again_agrees_and_a_cached_failure_that_passes_differs(store: Store) -> None:
    _cache(store, "test", FLAKY, True, flaky=True)
    cached = _cache(store, "mutant", RED, False)
    units = [_unit("test", FLAKY, "grade-v1-cached"), _unit("mutant", RED, "grade-v1-cached")]
    lab = Lab(units=units, fresh={FLAKY: False, RED: True})
    ledger = Ledger()

    with pytest.raises(audit.CILabVerdictMismatch) as raised:
        audit.promotion_audit(audit.Ports(store, lab, ledger, SLUG), HEAD)

    fresh = Verdict("mutant", RED, True, False, 1.0)
    assert raised.value.mismatches == (audit.Mismatch(cached, fresh, "grade-v1-cached", AUDIT_RUN),)
    assert [fields["title"] for _, fields, _ in ledger.added] == ["Lab verdict mismatch: mutant reuse disabled"]
    assert (
        f"{RED} cached failed by run grade-v1-cached, regraded passed by run {AUDIT_RUN}."
        in ledger.added[0][1]["description"]
    )


@pytest.mark.feature("lab-reuse-audit")
def test_units_without_a_cached_or_a_regraded_verdict_are_not_compared(store: Store) -> None:
    _cache(store, "test", LOST, True)
    units = [_unit("test", UNKNOWN, "grade-v1-cached"), _unit("test", LOST, "grade-v1-cached")]
    lab = Lab(units=units, fresh={UNKNOWN: False})
    ledger = Ledger()

    regrade = audit.promotion_audit(audit.Ports(store, lab, ledger, SLUG), HEAD)

    assert [verdict.key for verdict in regrade.verdicts] == [UNKNOWN]
    assert ledger.added == []


@pytest.mark.feature("lab-reuse-audit")
def test_each_differing_kind_is_disabled_and_gets_its_own_task(store: Store, client: fakeredis.FakeRedis) -> None:
    for key in (ONE, TWO, SAME):
        _cache(store, "test", key, True)
    _cache(store, "static", LINT, False)
    units = [
        _unit("test", ONE, "grade-v1-first"),
        _unit("test", TWO, "grade-v1-second"),
        _unit("test", SAME, "grade-v1-second"),
        _unit("static", LINT, "grade-v1-third"),
    ]
    fresh = {ONE: False, TWO: False, SAME: True, LINT: True}
    ledger = Ledger()

    with pytest.raises(audit.CILabVerdictMismatch) as raised:
        audit.promotion_audit(audit.Ports(store, Lab(units=units, fresh=fresh), ledger, SLUG), HEAD)

    assert [(m.cached.key, m.cached_run) for m in raised.value.mismatches] == [
        (ONE, "grade-v1-first"),
        (TWO, "grade-v1-second"),
        (LINT, "grade-v1-third"),
    ]
    assert str(raised.value) == "regraded verdicts differ from the cached ones for static, test"
    assert client.smembers(DISABLED) == {"static", "test"}
    titles = [fields["title"] for _, fields, _ in ledger.added]
    assert titles == ["Lab verdict mismatch: static reuse disabled", "Lab verdict mismatch: test reuse disabled"]
    tests = ledger.added[1][1]["description"]
    assert tests.startswith("The lab audit regraded test units differently from their cached verdicts: ")
    assert (
        f"{ONE} cached passed by run grade-v1-first, regraded failed by run {AUDIT_RUN}; "
        f"{TWO} cached passed by run grade-v1-second, regraded failed by run {AUDIT_RUN}. "
    ) in tests
    assert SAME not in tests
    assert ledger.added[0][1]["task"] != ledger.added[1][1]["task"]


@pytest.mark.feature("lab-reuse-audit")
def test_a_ledger_that_refuses_the_task_still_leaves_every_differing_kind_disabled(
    store: Store, client: fakeredis.FakeRedis
) -> None:
    _cache(store, "static", LINT, True)
    _cache(store, "test", ONE, True)
    units = [_unit("static", LINT, "grade-v1-first"), _unit("test", ONE, "grade-v1-first")]
    lab = Lab(units=units, fresh={LINT: False, ONE: False})

    with pytest.raises(LedgerDown):
        audit.promotion_audit(audit.Ports(store, lab, Ledger(down=True), SLUG), HEAD)

    assert client.smembers(DISABLED) == {"static", "test"}


@pytest.mark.feature("lab-audit-refiles-refused-mismatches")
def test_a_refused_mismatch_task_leaves_its_units_recorded_as_unfiled(store: Store) -> None:
    cached = _cache(store, "static", LINT, True)
    lab = Lab(units=[_unit("static", LINT, "grade-v1-cached")], fresh={LINT: False})

    with pytest.raises(LedgerDown):
        audit.promotion_audit(audit.Ports(store, lab, Ledger(down=True), SLUG), HEAD)

    fresh = Verdict("static", LINT, False, False, 1.0)
    assert audit.unfiled(store) == [audit.Mismatch(cached, fresh, "grade-v1-cached", AUDIT_RUN)]


@pytest.mark.feature("lab-audit-refiles-refused-mismatches")
def test_the_audit_after_a_refused_mismatch_task_files_it_with_the_same_units(store: Store) -> None:
    cached = _cache(store, "static", LINT, True)
    _cache(store, "test", ONE, True)
    units = [_unit("static", LINT, "grade-v1-cached"), _unit("test", ONE, "grade-v1-cached")]
    lab = Lab(units=units, fresh={LINT: False, ONE: True})
    ledger = Ledger(down=True)
    ports = audit.Ports(store, lab, ledger, SLUG)
    with pytest.raises(LedgerDown):
        audit.promotion_audit(ports, HEAD)
    ledger.down = False
    lab.run = "grade-v1-retry"

    with pytest.raises(audit.CILabVerdictMismatch) as raised:
        audit.promotion_audit(ports, HEAD)

    fresh = Verdict("static", LINT, False, False, 1.0)
    assert raised.value.mismatches == (audit.Mismatch(cached, fresh, "grade-v1-cached", AUDIT_RUN),)
    assert [(fields["task"], fields["title"]) for _, fields, _ in ledger.added] == [
        (
            f"lab-mismatch-static-{digest({'kind': 'static', 'run': 'grade-v1-retry'})[:16]}",
            "Lab verdict mismatch: static reuse disabled",
        )
    ]
    assert (
        f"{LINT} cached passed by run grade-v1-cached, regraded failed by run {AUDIT_RUN}. "
        in ledger.added[0][1]["description"]
    )
    assert audit.unfiled(store) == []
    assert audit.promotion_audit(ports, HEAD).run == "grade-v1-retry"
    assert len(ledger.added) == 1


@pytest.mark.feature("lab-audit-refiles-refused-mismatches")
def test_a_kind_filed_before_a_refused_kind_stays_filed_and_only_the_refused_kind_is_filed_again(
    store: Store,
) -> None:
    _cache(store, "static", LINT, True)
    cached = _cache(store, "test", ONE, True)
    units = [_unit("static", LINT, "grade-v1-cached"), _unit("test", ONE, "grade-v1-cached")]
    ledger = Ledger(takes=1)
    ports = audit.Ports(store, Lab(units=units, fresh={LINT: False, ONE: False}), ledger, SLUG)
    with pytest.raises(LedgerDown):
        audit.promotion_audit(ports, HEAD)

    refused = audit.Mismatch(cached, Verdict("test", ONE, False, False, 1.0), "grade-v1-cached", AUDIT_RUN)
    assert audit.unfiled(store) == [refused]
    ledger.takes = None
    with pytest.raises(audit.CILabVerdictMismatch) as raised:
        audit.promotion_audit(ports, HEAD)

    assert raised.value.mismatches == (refused,)
    assert [fields["title"] for _, fields, _ in ledger.added] == [
        "Lab verdict mismatch: static reuse disabled",
        "Lab verdict mismatch: test reuse disabled",
    ]


@pytest.mark.feature("lab-reuse-audit")
def test_the_sample_regrades_the_chosen_share_of_the_day_in_the_backfill_class(
    store: Store, client: fakeredis.FakeRedis
) -> None:
    hits = _hits(40)
    for hit in hits:
        _cache(store, hit.kind, hit.key, True)
    lab = Lab(days={DAY: hits}, fresh={hit.key: True for hit in hits})

    regrade = audit.sample(audit.Ports(store, lab, Ledger(), SLUG), DAY)

    chosen = audit.chosen(DAY, hits, audit.SHARE)
    assert audit.SHARE == 0.05
    assert len(chosen) == 2
    assert lab.graded == [(chosen, Priority.BACKFILL)]
    assert regrade.run == AUDIT_RUN
    assert client.smembers(DISABLED) == set()


@pytest.mark.feature("lab-reuse-audit")
def test_a_sampled_mismatch_disables_the_kind_and_files_the_task(store: Store, client: fakeredis.FakeRedis) -> None:
    hits = _hits(10)
    for hit in hits:
        _cache(store, hit.kind, hit.key, True)
    lab = Lab(days={DAY: hits}, fresh={hit.key: False for hit in hits})
    ledger = Ledger()

    with pytest.raises(audit.CILabVerdictMismatch) as raised:
        audit.sample(audit.Ports(store, lab, ledger, SLUG), DAY, 0.5)

    assert len(raised.value.mismatches) == 5
    assert lab.graded[0][1] == Priority.BACKFILL
    assert client.smembers(DISABLED) == {"test"}
    assert [fields["kind"] for _, fields, _ in ledger.added] == ["ci"]


@pytest.mark.feature("lab-reuse-audit")
def test_the_sample_is_deterministic_from_the_day_digest() -> None:
    hits = _hits(100)
    shuffled = list(hits)
    random.Random(7).shuffle(shuffled)

    chosen = audit.chosen(DAY, hits, 0.05)

    assert [hit.run for hit in chosen] == [
        "grade-v1-036",
        "grade-v1-086",
        "grade-v1-092",
        "grade-v1-006",
        "grade-v1-017",
    ]
    assert [hit.run for hit in audit.chosen("2026-11-19", hits, 0.05)] == [
        "grade-v1-078",
        "grade-v1-045",
        "grade-v1-022",
        "grade-v1-081",
        "grade-v1-033",
    ]
    assert audit.chosen(DAY, shuffled, 0.05) == chosen
    assert audit.chosen("2026-11-19", hits, 0.05) != chosen
    assert len(audit.chosen(DAY, _hits(30), 0.05)) == math.ceil(30 * 0.05) == 2
    assert len(audit.chosen(DAY, _hits(3), 0.05)) == 1
    assert audit.chosen(DAY, [], 0.05) == []


@pytest.mark.feature("lab-reuse-audit")
def test_the_same_unit_hit_on_two_heads_ranks_by_its_head() -> None:
    first, second = _unit("test", ONE, "grade-v1-one"), _unit("test", ONE, "grade-v1-one", OTHER)

    assert audit.chosen(DAY, [first, second], 0.5) == [first]
    assert audit.chosen(DAY, [second, first], 0.5) == [first]
