"""The claims gate on the events path: hub admission applies the three life cap, its gate modes and its report."""

import json
from typing import Any

import pytest

from scripts.gates.claims import refusal
from scripts.hub import claims
from scripts.swarm.store import AgentRecord
from tests.helpers import store_seed
from tests.hub.test_admission import SLUG, ask
from tests.hub.test_admission import rig as rig
from tests.hub.test_claim_lives import started


def gate_rows() -> list[dict[str, Any]]:
    """The rows the swarm gate log holds for the test swarm."""
    from scripts.gates.log import gate_log_path

    path = gate_log_path(SLUG)
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def lives(rig: Any, n: int) -> None:
    """Spends ``n`` lives on t1 and records the ledger comments the claims gate posts."""
    rig.ledger.comments = []
    rig.ledger.comment = lambda slug, task_id, text, by: rig.ledger.comments.append((slug, task_id, text, by))
    for _ in range(n):
        store_seed.count_claim(rig.store, SLUG, "t1")


@pytest.mark.feature("swarm-events-claim-cap")
def test_a_third_life_is_admitted_counted_and_logs_no_gate_row(rig: Any) -> None:
    lives(rig, 2)
    admitted = rig.admitter.admit(SLUG, ask())
    claims.Claims(rig.store).handle(started(admitted))
    assert (rig.rows["t1"]["state"], rig.store.claims(SLUG, "t1")) == ("claimed", 3)
    assert gate_rows() == []


@pytest.mark.feature("swarm-events-claim-cap")
def test_a_fourth_claim_logs_one_deny_row_for_the_swarm(rig: Any) -> None:
    lives(rig, 3)
    rig.store.put_handoff(SLUG, "t1", "handoff body", envelope={"reason": "recycle"})
    assert rig.admitter.admit(SLUG, ask()) is None
    assert [(r["gate"], r["kind"], r["agent"], r["task"], r["reason"]) for r in gate_rows()] == [
        ("claims", "deny", "swarm", "t1", refusal(3, "recycle", "none", SLUG, "t1"))
    ]


@pytest.mark.feature("swarm-events-claim-cap")
def test_blocking_restarts_the_count_so_a_reopen_is_admitted_again(rig: Any) -> None:
    lives(rig, 3)
    assert rig.admitter.admit(SLUG, ask()) is None
    assert (rig.rows["t1"]["state"], rig.store.claims(SLUG, "t1")) == ("blocked", 0)
    rig.rows["t1"]["state"] = "open"
    admitted = rig.admitter.admit(SLUG, ask())
    assert admitted is not None
    assert (rig.rows["t1"]["state"], rig.rows["t1"]["claimed_by"]) == ("claimed", admitted["name"])


@pytest.mark.feature("swarm-events-claim-cap")
def test_without_a_pending_handoff_the_refusal_says_none(rig: Any) -> None:
    lives(rig, 4)
    assert rig.admitter.admit(SLUG, ask()) is None
    assert rig.ledger.comments == [(SLUG, "t1", refusal(4, "none", "none", SLUG, "t1"), "swarm")]


@pytest.mark.feature("swarm-events-claim-cap")
def test_observe_admits_the_fourth_claim_and_logs_the_would_be_deny(rig: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIHOOKS_GATE_CLAIMS", "enforce")
    rig.store.update(SLUG, gates={"claims": "observe"})
    lives(rig, 3)
    admitted = rig.admitter.admit(SLUG, ask())
    claims.Claims(rig.store).handle(started(admitted))
    assert (rig.rows["t1"]["state"], rig.ledger.comments, rig.store.claims(SLUG, "t1")) == ("claimed", [], 4)
    assert [(r["kind"], r["reason"]) for r in gate_rows()] == [("observe", refusal(3, "none", "none", SLUG, "t1"))]


@pytest.mark.feature("swarm-events-claim-cap")
def test_off_skips_the_cap_and_logs_nothing(rig: Any) -> None:
    rig.store.update(SLUG, gates={"claims": "off"})
    lives(rig, 3)
    assert rig.admitter.admit(SLUG, ask()) is not None
    assert gate_rows() == []


@pytest.mark.feature("swarm-events-claim-cap")
def test_admission_ignores_a_mode_in_its_own_environment(rig: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIHOOKS_GATE_CLAIMS", "off")
    lives(rig, 3)
    assert rig.admitter.admit(SLUG, ask()) is None
    assert rig.rows["t1"]["state"] == "blocked"


@pytest.mark.feature("swarm-events-claim-cap")
def test_a_crashed_cap_admits_the_claim_and_logs_a_fail_open_row(rig: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(slug: str, task: str) -> int:
        raise ConnectionError("redis blip")

    monkeypatch.setattr(rig.store, "claims", broken)
    assert rig.admitter.admit(SLUG, ask()) is not None
    assert [(r["gate"], r["kind"], r["agent"], r["task"], r["reason"]) for r in gate_rows()] == [
        ("claims", "fail-open", "swarm", "t1", "ConnectionError: redis blip")
    ]


@pytest.mark.feature("swarm-events-claim-cap")
def test_a_failed_launch_spends_no_life_and_the_start_after_it_counts_once(rig: Any) -> None:
    admitted = rig.admitter.admit(SLUG, ask())
    assert rig.admitter.failed(SLUG, ask(harness="", reason="herdr down"))
    assert (rig.store.claims(SLUG, "t1"), rig.store.launch_failure(SLUG, "t1")) == (0, "herdr down")
    claims.Claims(rig.store).handle(started(admitted))
    assert (rig.store.claims(SLUG, "t1"), rig.store.claims(SLUG, "t2")) == (1, 0)
    assert (rig.rows["t1"]["state"], rig.rows["t1"]["claimed_by"]) == ("claimed", admitted["name"])


@pytest.mark.feature("swarm-events-claim-cap")
@pytest.mark.parametrize(
    ("failure_at", "expected"),
    [
        (1_792_439_999_999, "none"),
        (1_792_440_000_000, "2026-10-19 20:00:00 UTC: no space left on device"),
        (1_792_440_001_000, "2026-10-19 20:00:01 UTC: no space left on device"),
    ],
)
def test_the_refusal_dates_only_failures_within_the_counted_claims(
    rig: Any, failure_at: int, expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    store = rig.store
    for name, at in (
        ("previous", 1_792_008_000_000),
        ("third", 1_792_440_003_000),
        ("first", 1_792_440_000_000),
        ("second", 1_792_440_002_000),
    ):
        store_seed.record_launch(store, SLUG, AgentRecord(name, "eng", "t1", started_at=at), "started")
    store.reset_claims(SLUG, "t1")
    lives(rig, 3)
    store_seed.record_launch(
        store, SLUG, AgentRecord("failed", "eng", "t1", started_at=failure_at), "failed", "no space left on device"
    )
    store.note_launch_failure(SLUG, "t1", "no space left on device")
    for name, task, at, state in (
        ("older_failure", "t1", failure_at - 1, "failed"),
        ("other_task", "t2", 1_792_440_100_000, "failed"),
        ("other_life", "t2", 1_792_440_101_000, "started"),
        ("waiting", "t1", 1_792_440_102_000, "waiting"),
        ("pending", "t1", 1_792_440_103_000, "pending"),
    ):
        store_seed.record_launch(store, SLUG, AgentRecord(name, "eng", task, started_at=at), state, "different error")
    original = store.launches(SLUG)
    assert rig.admitter.admit(SLUG, ask()) is None
    [(_, _, report, _)] = rig.ledger.comments
    assert f"Last launch failure: {expected}. Change its scope" in report
    assert f"hub: events claim cap swarm={SLUG} task=t1 hive=hive-a: blocked t1: {report}" in (
        capsys.readouterr().err.splitlines()
    )
    assert rig.rows["t1"]["state"] == "blocked"
    assert all(row in store.launches(SLUG) for row in original)
