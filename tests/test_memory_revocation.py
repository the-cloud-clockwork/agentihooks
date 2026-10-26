"""Tests for SV2-RET-02 deletion and access revocation propagation."""

import hashlib
import json
from pathlib import Path

import pytest

from hooks.memory import revocation
from hooks.memory.api.ingest import Grant
from hooks.memory.catalog import Catalog, Conflict, SessionSource
from tests import sv2_ret02_cases as cases

pytestmark = pytest.mark.unit
EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-RET-02"
DOMAIN = "corpus-a"
GRANT = Grant("p", DOMAIN, frozenset({"proj"}))


@pytest.fixture
def catalog(tmp_path):
    found = Catalog(tmp_path / "catalog.sqlite3")
    for uid in ("s1", "s2"):
        found.register(SessionSource(DOMAIN, uid, "g", "claude", f"origin-{uid}", "retain-until-deleted"))
    yield found
    found.close()


@pytest.fixture
def propagator(catalog):
    return revocation.Propagator(catalog)


def test_set_state_records_a_tombstone_in_order(catalog):
    assert catalog.set_state(DOMAIN, "s1", "deleted", 1, 10.0) == 2
    assert catalog.set_state(DOMAIN, "s2", "restricted", 1, 11.0) == 2
    assert catalog.tombstones(DOMAIN) == [
        {"sequence": 1, "session_uid": "s1", "state": "deleted", "revision": 2, "recorded_at": 10.0},
        {"sequence": 2, "session_uid": "s2", "state": "restricted", "revision": 2, "recorded_at": 11.0},
    ]
    assert [item["sequence"] for item in catalog.tombstones(DOMAIN, 1)] == [2]
    assert catalog.tombstones("other") == []


def test_a_stale_state_change_records_no_tombstone(catalog):
    with pytest.raises(Conflict):
        catalog.set_state(DOMAIN, "s1", "deleted", 5, 10.0)
    assert catalog.tombstones(DOMAIN) == []


def test_set_state_stamps_the_wall_clock_by_default(catalog, monkeypatch):
    monkeypatch.setattr("hooks.memory.catalog.time.time", lambda: 77.0)
    catalog.set_state(DOMAIN, "s1", "deleted", 1)
    assert catalog.tombstones(DOMAIN)[0]["recorded_at"] == 77.0


def test_tombstone_sequences_are_per_domain(tmp_path):
    catalog = Catalog(tmp_path / "c.sqlite3")
    for domain in ("a", "b"):
        catalog.register(SessionSource(domain, "s", "g", "claude", "o", "retain-until-deleted"))
        catalog.set_state(domain, "s", "deleted", 1, 1.0)
    assert [item["sequence"] for item in catalog.tombstones("b")] == [1]


def test_packs_citing_a_revoked_session_are_dropped_and_withheld(catalog, propagator):
    propagator.put_pack(DOMAIN, "p1", {"previews": ["x"]}, ["s1", "s2", "s1"])
    propagator.put_pack(DOMAIN, "p2", {"previews": ["y"]}, ["s2"])
    assert propagator.pack(GRANT, DOMAIN, "p1") == {"previews": ["x"]}
    catalog.set_state(DOMAIN, "s1", "deleted", 1, 10.0)
    with pytest.raises(revocation.Withheld) as raised:
        propagator.pack(GRANT, DOMAIN, "p1")
    assert str(raised.value) == revocation.WITHHELD
    assert propagator.exposures(DOMAIN, "s1") == {"context_pack": 1}
    assert propagator.replay(DOMAIN, "packs", 12.5) == [
        {"consumer": "packs", "sequence": 1, "session_uid": "s1", "references": ["p1"]}
    ]
    assert propagator.exposures(DOMAIN, "s1") == {}
    assert propagator.pack(GRANT, DOMAIN, "p2") == {"previews": ["y"]}


@pytest.mark.parametrize("domain,pack_id", [(DOMAIN, "missing"), ("other", "p2")])
def test_an_unknown_or_foreign_pack_is_withheld(propagator, domain, pack_id):
    propagator.put_pack(DOMAIN, "p2", {}, ["s2"])
    propagator.put_pack("other", "p2", {}, ["s2"])
    with pytest.raises(revocation.Withheld) as raised:
        propagator.pack(GRANT, domain, pack_id)
    assert str(raised.value) == revocation.WITHHELD


def test_lessons_keep_permitted_support_and_redact_without_it(catalog, propagator):
    propagator.put_lesson(DOMAIN, "only", "text one", ["s1"])
    propagator.put_lesson(DOMAIN, "shared", "text two", ["s2", "s1"])
    propagator.put_lesson(DOMAIN, "other", "text three", ["s2"])
    assert propagator.lesson(GRANT, DOMAIN, "shared") == {
        "lesson_id": "shared",
        "state": "inferred",
        "text": "text two",
        "support": ["s1", "s2"],
    }
    catalog.set_state(DOMAIN, "s1", "restricted", 1, 10.0)
    assert propagator.lesson(GRANT, DOMAIN, "only") == {
        "lesson_id": "only",
        "state": "redacted",
        "text": None,
        "support": [],
    }
    assert propagator.lesson(GRANT, DOMAIN, "shared")["support"] == ["s2"]
    assert propagator.replay(DOMAIN, "lessons", 11.0)[0]["references"] == ["only", "shared"]
    rows = catalog.db.execute("SELECT lesson_id, text, support, state FROM derived_lesson ORDER BY lesson_id")
    assert rows.fetchall() == [
        ("only", None, "[]", "redacted"),
        ("other", "text three", '["s2"]', "inferred"),
        ("shared", "text two", '["s2"]', "revalidated"),
    ]
    catalog.set_state(DOMAIN, "s1", "retained", 2, 12.0)
    assert propagator.lesson(GRANT, DOMAIN, "only")["state"] == "redacted"


@pytest.mark.parametrize("domain,lesson_id", [(DOMAIN, "missing"), ("other", "only")])
def test_an_unknown_or_foreign_lesson_is_withheld(propagator, domain, lesson_id):
    propagator.put_lesson("other", "only", "t", ["s1"])
    with pytest.raises(revocation.Withheld) as raised:
        propagator.lesson(GRANT, domain, lesson_id)
    assert str(raised.value) == revocation.WITHHELD


def test_replay_reads_only_tombstones_after_its_own_cursor(catalog, propagator, monkeypatch):
    catalog.set_state(DOMAIN, "s1", "deleted", 1, 10.0)
    propagator.replay(DOMAIN, "packs", 10.0)
    catalog.set_state(DOMAIN, "s2", "deleted", 1, 11.0)
    seen = []
    original = catalog.tombstones
    monkeypatch.setattr(
        catalog, "tombstones", lambda domain, after=0: seen.append((domain, after)) or original(domain, after)
    )
    propagator.replay(DOMAIN, "packs", 12.0)
    propagator.replay(DOMAIN, "lessons", 12.0)
    assert seen == [(DOMAIN, 1), (DOMAIN, 0)]


def test_search_projections_of_a_revoked_session_are_removed(catalog, propagator):
    for table, values in (
        ("search_chunk", (DOMAIN, "s1", "v", "c2", "g", 1, "{}")),
        ("search_chunk", (DOMAIN, "s1", "v", "c1", "g", 1, "{}")),
        ("search_chunk", (DOMAIN, "s2", "v", "c3", "g", 1, "{}")),
        ("chunk_vector", (DOMAIN, "ix", "c1", "s1", "v", "h", "m", 1, "[1]")),
        ("chunk_cutover", (DOMAIN, "s1", "v", 1)),
    ):
        catalog.db.execute(f"INSERT INTO {table} VALUES ({', '.join('?' * len(values))})", values)
    assert propagator.exposures(DOMAIN, "s1") == {"search_chunk": 2, "chunk_cutover": 1, "chunk_vector": 1}
    catalog.set_state(DOMAIN, "s1", "deleted", 1, 10.0)
    assert propagator.replay(DOMAIN, "search", 10.0)[0]["references"] == ["c1", "c2"]
    assert propagator.exposures(DOMAIN, "s1") == {}
    assert propagator.exposures(DOMAIN, "s2") == {"search_chunk": 1}


def test_replay_records_lag_and_applies_each_tombstone_once(catalog, propagator):
    catalog.set_state(DOMAIN, "s1", "deleted", 1, 10.0)
    catalog.set_state(DOMAIN, "s2", "deleted", 1, 11.0)
    assert len(propagator.catch_up(DOMAIN, 14.0)) == 6
    assert propagator.catch_up(DOMAIN, 15.0) == []
    assert propagator.through(DOMAIN, "lessons") == 2
    assert propagator.through(DOMAIN, "nobody") == 0
    assert propagator.lags(DOMAIN)[:2] == [
        {"consumer": "lessons", "sequence": 1, "session_uid": "s1", revocation.METRIC: 4.0},
        {"consumer": "lessons", "sequence": 2, "session_uid": "s2", revocation.METRIC: 3.0},
    ]
    assert len(propagator.lags(DOMAIN)) == 6


def test_a_stale_step_changes_nothing(catalog, propagator):
    catalog.set_state(DOMAIN, "s1", "deleted", 1, 10.0)
    propagator.replay(DOMAIN, "packs", 10.0)
    assert propagator._step(DOMAIN, "packs", catalog.tombstones(DOMAIN)[0], 99.0) is None
    assert propagator.lags(DOMAIN)[0][revocation.METRIC] == 0.0


def test_a_tombstone_for_a_regranted_session_removes_nothing(catalog, propagator):
    propagator.put_pack(DOMAIN, "p1", {}, ["s1"])
    catalog.set_state(DOMAIN, "s1", "deleted", 1, 10.0)
    catalog.set_state(DOMAIN, "s1", "retained", 2, 11.0)
    assert [item["references"] for item in propagator.replay(DOMAIN, "packs", 12.0)] == [[], []]
    assert propagator.pack(GRANT, DOMAIN, "p1") == {}


def test_notices_list_references_in_order_and_packs_store_sorted_sessions(catalog, propagator):
    propagator.put_pack(DOMAIN, "pz", {}, ["s2", "s1", "s2"])
    propagator.put_pack(DOMAIN, "pa", {}, ["s1"])
    propagator.put_lesson(DOMAIN, "zeta", "t", ["s2", "s1", "s1"])
    propagator.put_lesson(DOMAIN, "alpha", "t", ["s1"])
    stored = catalog.db.execute("SELECT pack_id, sessions FROM context_pack ORDER BY pack_id").fetchall()
    assert stored == [("pa", '["s1"]'), ("pz", '["s1", "s2"]')]
    assert catalog.db.execute("SELECT support FROM derived_lesson WHERE lesson_id = 'zeta'").fetchone() == (
        '["s1", "s2"]',
    )
    catalog.set_state(DOMAIN, "s1", "deleted", 1, 10.0)
    notices = propagator.catch_up(DOMAIN, 10.1234567)
    assert [item["references"] for item in notices[1:]] == [["pa", "pz"], ["alpha", "zeta"]]
    assert [item[revocation.METRIC] for item in propagator.lags(DOMAIN)] == [0.123457] * 3


def test_an_unknown_consumer_is_refused(propagator):
    with pytest.raises(ValueError) as raised:
        propagator.replay(DOMAIN, "vectors", 1.0)
    assert str(raised.value) == revocation.UNKNOWN_CONSUMER


def test_an_interrupted_step_rolls_back_its_changes(catalog):
    flaky = cases.Flaky(catalog)
    flaky.put_lesson(DOMAIN, "only", "t", ["s1"])
    catalog.set_state(DOMAIN, "s1", "deleted", 1, 10.0)
    with pytest.raises(cases.Interrupted):
        flaky.replay(DOMAIN, "lessons", 11.0)
    assert flaky.through(DOMAIN, "lessons") == 0
    row = catalog.db.execute("SELECT text, state FROM derived_lesson").fetchone()
    assert row == ("t", "inferred")


@pytest.mark.parametrize("case", ["A", "B", "C"])
def test_package_cases_match_committed_evidence(tmp_path, case):
    observed = cases.run_case(case, tmp_path)
    path = EVIDENCE / f"{case.lower()}-result.json"
    expected = json.loads(path.read_text()) if path.exists() else None
    assert observed == expected, json.dumps(observed, sort_keys=True)


def test_package_record_and_manifest_match_the_fixture_inputs():
    record = json.loads((EVIDENCE / "result.json").read_text())
    manifest = json.loads((EVIDENCE / "manifest.json").read_text())
    base = cases.FIXTURE.parent.parent / cases.fixture()["base"]
    assert manifest["input_sha256"] == {
        cases.FIXTURE.name: hashlib.sha256(cases.FIXTURE.read_bytes()).hexdigest(),
        base.name: hashlib.sha256(base.read_bytes()).hexdigest(),
    }
    assert manifest["fixture_inputs"] == record["fixture_inputs"] == cases.FIXTURE_INPUTS
    assert manifest["tested_commit"] == record["tested_commit"]
