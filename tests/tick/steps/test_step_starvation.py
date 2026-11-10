"""The step starvation tick step records each step's last completed run and alarms once a step misses three windows."""

import hashlib
import json
import time
from importlib import import_module
from typing import Any

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import cli, ledger_probe, timing
from scripts.swarm.health import step_starvation
from scripts.swarm.store import RedisStore
from scripts.tick import engine, pool, registry
from scripts.tick.deadline import Deadline
from scripts.tick.sites import _GraphView
from scripts.tick.step import Context

pytestmark = pytest.mark.unit

SLUG = "sw"
MASTER = "master@sw"
START = 1_794_000_000_000
MINUTE = 60_000
WAITS = "waits end"
WINDOWS = {WAITS: 60.0, "spawn": 60.0}


@pytest.fixture
def store() -> RedisStore:
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def journal(store: RedisStore, at: int, step: str, outcome: str, **flags: bool) -> None:
    row = {"step": step, "outcome": outcome, "abandoned": False, "rested": False, **flags}
    store.redis.xadd(store.key(SLUG, "tick:records"), {"record": json.dumps(row)}, id=f"{at}-0")


def items(store: RedisStore) -> list[Any]:
    return [item for item in InboxStore(store.redis).inbox(MASTER) if item.sender == "swarm"]


def replay(store: RedisStore, passes: int) -> list[list[str]]:
    """Replay ``passes`` passes where waits end is abandoned at its budget every pass and spawn completes."""
    lines = [step_starvation.step(store, SLUG, START, WINDOWS)]
    for index in range(1, passes + 1):
        at = START + index * MINUTE
        journal(store, at, WAITS, engine.LATE, abandoned=True)
        journal(store, at + 1, "spawn", engine.OK)
        lines.append(step_starvation.step(store, SLUG, at + 1_000, WINDOWS))
    return lines


@pytest.mark.feature("tick-step-starvation")
def test_step_starvation() -> None:
    """The step starvation step registers in group closing on the closing lane with its feature and ports."""
    import_module("scripts.tick.steps.report")
    found = {step.name: step for step in registry.steps()}["step starvation"]
    assert (found.group, found.lane, found.feature, found.effects) == (
        "closing",
        "closing",
        "tick-step-starvation",
        frozenset({"inbox"}),
    )


@pytest.mark.feature("tick-step-starvation")
def test_a_pass_history_where_waits_end_never_completes_alarms_the_master_once(store: RedisStore) -> None:
    """Replayed passes where waits end is abandoned every pass alarm the master once, after three windows."""
    lines = replay(store, 6)
    assert lines[:4] == [[], [], [], []]
    assert lines[4] == [f"step starvation: alarmed {MASTER}: Tick step waits end has not completed for 3 minutes"]
    assert lines[5:] == [[], []]
    [item] = items(store)
    assert item.text.startswith("URGENT: Tick step waits end has not completed for 3 minutes on swarm sw.")
    assert "Last completed run: never since the watch began" in item.text


@pytest.mark.feature("tick-step-starvation")
def test_swarm_status_lists_the_starved_step_with_its_last_completion_and_passes_since(store: RedisStore) -> None:
    """The health finding names the starved step, its last completion and the passes since, and only that step."""
    journal(store, START - MINUTE, WAITS, engine.OK)
    step_starvation.step(store, SLUG, START - MINUTE - 1, WINDOWS)
    replay(store, 4)
    [found] = step_starvation.findings(store, SLUG, START + 4 * MINUTE + 1_000)
    assert found.id == "step-starvation/waits-end"
    assert found.summary == "Tick step waits end has not completed for 5 minutes"
    assert found.evidence == (
        "Last completed run: 06 November 21:19 UTC",
        "4 passes since ran it without a completed run, the first at 06 November 21:21 UTC",
        "Window 60s from budgets.FLOORS; three windows is 180s",
    )
    assert (found.threshold, found.measure) == ("three windows", 4)


@pytest.mark.feature("tick-step-starvation")
def test_a_step_one_millisecond_short_of_three_windows_is_not_starved(store: RedisStore) -> None:
    """A step whose first miss is one millisecond short of three windows raises no finding and no item."""
    replay(store, 3)
    at = START + MINUTE + 3 * MINUTE - 1
    assert step_starvation.step(store, SLUG, at, WINDOWS) == []
    assert step_starvation.findings(store, SLUG, at) == []
    [found] = step_starvation.findings(store, SLUG, at + 1)
    assert found.measure == 3
    assert found.summary == "Tick step waits end has not completed for 3 minutes"


@pytest.mark.feature("tick-step-starvation")
def test_a_completed_run_clears_the_starvation_and_rested_rows_never_start_it(store: RedisStore) -> None:
    """A late run that still ended clears the step; rested rows never count as misses."""
    replay(store, 5)
    at = START + 6 * MINUTE
    journal(store, at, WAITS, engine.LATE)
    for index in range(1, 6):
        journal(store, at + index * MINUTE, "spawn", "skipped", rested=True)
    assert step_starvation.step(store, SLUG, at + 10 * MINUTE, WINDOWS) == []
    assert step_starvation.findings(store, SLUG, at + 10 * MINUTE) == []
    assert step_starvation.read(store, SLUG)["steps"] == {
        WAITS: {"done": at},
        "spawn": {"done": START + 5 * MINUTE + 1},
    }


@pytest.mark.feature("tick-step-starvation")
def test_failed_held_cut_and_dropped_runs_are_misses(store: RedisStore) -> None:
    """Every row that ran without completing counts one miss from the first."""
    step_starvation.step(store, SLUG, START, WINDOWS)
    for index, outcome in enumerate((engine.FAILED, engine.HELD, "cut", "skipped"), start=1):
        journal(store, START + index * MINUTE, WAITS, outcome)
    lines = step_starvation.step(store, SLUG, START + 5 * MINUTE, WINDOWS)
    assert step_starvation.read(store, SLUG)["steps"][WAITS] == {"missed": START + MINUTE, "misses": 4}
    assert len(lines) == 1


@pytest.mark.feature("tick-step-starvation")
def test_the_registered_step_records_runs_and_alarms_with_each_step_window(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registered step reads the pass records with each registered step's window and alarms the master."""
    import_module("scripts.tick.steps.report")
    monkeypatch.setattr(cli, "now_ms", lambda: START + 5 * MINUTE)
    store.redis.set(
        store.key(SLUG, step_starvation.KEY), json.dumps({"cursor": f"{START}-0", "steps": {}, "windows": {}})
    )
    for index in range(1, 5):
        journal(store, START + index * MINUTE, WAITS, engine.LATE, abandoned=True)
    found = next(step for step in registry.steps() if step.name == "step starvation")
    state = {"store": store, "slug": SLUG, "box": {}}
    with pool.running(critical_s=1.0, slow_s=1.0):
        records = engine.run_graph((found,), Context(_GraphView(state, {}), Deadline.after(5.0)))
    assert records[0].outcome == engine.OK
    saved = step_starvation.read(store, SLUG)
    assert saved["windows"][WAITS] == 60.0
    assert saved["windows"]["doc report"] == 600.0
    assert len(items(store)) == 1


def counted_reads(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    reads: list[str] = []
    real = store.redis.get

    def get(key: str) -> Any:
        reads.append(key)
        return real(key)

    monkeypatch.setattr(store.redis, "get", get)
    return reads


@pytest.mark.feature("tick-step-starvation")
def test_swarm_status_reads_the_starvation_state_once_per_tick_pass(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every findings call of one tick pass shares one read; a new pass reads again; outside a pass every call reads."""
    replay(store, 4)
    reads = counted_reads(store, monkeypatch)
    at = START + 4 * MINUTE + 1_000
    token = timing.PASS.set({"pass_id": "p1", "start_ms": START})
    try:
        first = step_starvation.findings(store, SLUG, at)
        assert step_starvation.findings(store, SLUG, at) == first and len(first) == 1
        assert len(reads) == 1
    finally:
        timing.PASS.reset(token)
    token = timing.PASS.set({"pass_id": "p2", "start_ms": START})
    try:
        step_starvation.findings(store, SLUG, at)
        assert len(reads) == 2
        assert [key[0] for key in step_starvation._PASS_READS] == ["p2"]
    finally:
        timing.PASS.reset(token)
    step_starvation.findings(store, SLUG, at)
    step_starvation.findings(store, SLUG, at)
    assert len(reads) == 4


@pytest.mark.feature("tick-step-starvation")
def test_inside_one_reap_the_ledger_probe_alert_is_read_once_per_swarm(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside read_once every holding call after the first answers from it; outside it each call reads the alert."""
    store.redis.set(ledger_probe._key(store, SLUG), json.dumps({"alert": True}))
    reads = counted_reads(store, monkeypatch)
    with ledger_probe.read_once():
        assert [ledger_probe.holding(store, SLUG) for _ in range(5)] == [True] * 5
        assert ledger_probe.holding(store, "other") is False
    assert len(reads) == 2
    store.redis.set(ledger_probe._key(store, SLUG), json.dumps({}))
    assert ledger_probe.holding(store, SLUG) is False
    assert ledger_probe.holding(store, SLUG) is False
    assert len(reads) == 4


@pytest.mark.feature("tick-step-starvation")
def test_finding_times_read_in_utc_whatever_the_host_zone(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """A host in another time zone still names the last completion and first miss in UTC."""
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()
    try:
        journal(store, START - MINUTE, WAITS, engine.OK)
        step_starvation.step(store, SLUG, START - MINUTE - 1, WINDOWS)
        replay(store, 4)
        [found] = step_starvation.findings(store, SLUG, START + 4 * MINUTE + 1_000)
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
    assert found.evidence[:2] == (
        "Last completed run: 06 November 21:19 UTC",
        "4 passes since ran it without a completed run, the first at 06 November 21:21 UTC",
    )


@pytest.mark.feature("tick-step-starvation")
def test_one_tick_pass_keeps_the_read_of_every_swarm_it_asked(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reads for two swarms in one pass are both kept; asking either again reads nothing."""
    replay(store, 4)
    reads = counted_reads(store, monkeypatch)
    at = START + 4 * MINUTE + 1_000
    token = timing.PASS.set({"pass_id": "p1", "start_ms": START})
    try:
        assert len(step_starvation.findings(store, SLUG, at)) == 1
        assert step_starvation.findings(store, "other", at) == []
        assert len(step_starvation.findings(store, SLUG, at)) == 1
        assert step_starvation.findings(store, "other", at) == []
    finally:
        timing.PASS.reset(token)
    assert len(reads) == 2
    assert sorted(step_starvation._PASS_READS) == [("p1", "other"), ("p1", SLUG)]


@pytest.mark.feature("tick-step-starvation")
def test_a_swarm_with_nothing_saved_has_no_starvation(store: RedisStore) -> None:
    """With no saved state there is no window and no starved step, and no finding."""
    assert step_starvation.starved({}, START) == []
    assert step_starvation.starved({"steps": {WAITS: {"missed": START, "misses": 9}}}, START + 60 * MINUTE) == []
    assert step_starvation.findings(store, SLUG, START) == []


@pytest.mark.feature("tick-step-starvation")
def test_swarm_status_findings_list_the_starved_step_at_the_status_clock(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Swarm status lists a starved step's finding, measured against its own clock."""
    from scripts.swarm import status
    from scripts.swarm.store import SwarmConfig

    config = SwarmConfig(SLUG, "/repo", max_eng=1, max_ci=0, autonomy="full")
    store.create(config)
    store.redis.set(store.key(SLUG, "merge-stall"), json.dumps({"tried": START, "queue": 0}))
    replay(store, 4)
    monkeypatch.setattr(status, "now_ms", lambda: START + 6 * MINUTE)
    rows = [row for row in status.findings(store, SLUG, config, [], []) if row["kind"] == "step starvation"]
    assert [(row["subject"], row["summary"]) for row in rows] == [
        ("waits-end", "Tick step waits end has not completed for 5 minutes")
    ]


@pytest.mark.feature("tick-step-starvation")
def test_the_alarm_item_carries_its_id_ref_and_every_evidence_line(store: RedisStore) -> None:
    """The master's item is keyed by the step and its first miss, names its ref and joins every evidence line."""
    replay(store, 4)
    [item] = items(store)
    digest = hashlib.sha256(f"step-starvation:{SLUG}:{WAITS}:{START + MINUTE}".encode()).hexdigest()[:12]
    assert (item.id, item.ref) == (digest, f"step-starvation:{SLUG}:{WAITS}")
    assert item.text == (
        "URGENT: Tick step waits end has not completed for 3 minutes on swarm sw. Evidence: "
        "Last completed run: never since the watch began; "
        "4 passes since ran it without a completed run, the first at 06 November 21:21 UTC; "
        "Window 60s from budgets.FLOORS; three windows is 180s."
    )


@pytest.mark.feature("tick-step-starvation")
def test_a_step_already_alarmed_never_hides_the_next_starved_step(store: RedisStore) -> None:
    """A starved step alarmed earlier is passed over and a step that starves later still alarms the master."""
    step_starvation.step(store, SLUG, START, WINDOWS)
    for index in range(1, 9):
        at = START + index * MINUTE
        journal(store, at, "spawn", engine.LATE, abandoned=True)
        journal(store, at + 1, WAITS, engine.OK if index < 5 else engine.FAILED)
        step_starvation.step(store, SLUG, at + 1_000, WINDOWS)
    assert sorted(item.ref for item in items(store)) == [
        f"step-starvation:{SLUG}:spawn",
        f"step-starvation:{SLUG}:{WAITS}",
    ]


@pytest.mark.feature("tick-step-starvation")
def test_reading_again_with_no_new_records_counts_nothing_twice(store: RedisStore) -> None:
    """The record at the cursor was already counted; a read with nothing newer leaves every step as it was."""
    step_starvation.step(store, SLUG, START, WINDOWS)
    journal(store, START + MINUTE, WAITS, engine.FAILED)
    step_starvation.step(store, SLUG, START + MINUTE + 1_000, WINDOWS)
    step_starvation.step(store, SLUG, START + MINUTE + 2_000, WINDOWS)
    saved = step_starvation.read(store, SLUG)
    assert (saved["cursor"], saved["steps"]) == (
        f"{START + MINUTE}-0",
        {WAITS: {"missed": START + MINUTE, "misses": 1}},
    )


@pytest.mark.feature("tick-step-starvation")
def test_one_read_takes_only_the_newest_records_up_to_its_limit(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past the read limit the oldest unread records are skipped and the newest are counted."""
    monkeypatch.setattr(step_starvation, "READ_LIMIT", 3)
    step_starvation.step(store, SLUG, START, WINDOWS)
    for index in range(1, 6):
        journal(store, START + index * MINUTE, WAITS, engine.FAILED)
    step_starvation.step(store, SLUG, START + 6 * MINUTE, WINDOWS)
    assert step_starvation.read(store, SLUG)["steps"][WAITS] == {"missed": START + 3 * MINUTE, "misses": 3}


@pytest.mark.feature("tick-step-starvation")
def test_a_stream_entry_id_reads_as_its_millisecond_and_sequence() -> None:
    """An entry id gives its millisecond and sequence; anything else is refused by name."""
    assert step_starvation._position("1794000000000-7") == (1_794_000_000_000, 7)
    with pytest.raises(ValueError, match="^not a stream entry id: 1794000000000$"):
        step_starvation._position("1794000000000")
