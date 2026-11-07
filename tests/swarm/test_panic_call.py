"""Degradation held an hour asks the front door master whether it is a panic mode call."""

import json
from typing import Any

import pytest

from scripts.gates import log as gate_log
from scripts.inbox.store import InboxStore
from scripts.swarm import blockers
from scripts.swarm.health import early_ends, panic_call, spawn_stall

pytestmark = pytest.mark.unit

SLUG = "sw"
MINUTE = 60_000
HOUR = 60 * MINUTE
START = 1_800_000_000_000


@pytest.fixture
def store(tmp_path, monkeypatch):
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig

    monkeypatch.setenv("HOME", str(tmp_path))
    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig(SLUG, "/repo", max_eng=0, max_ci=0))
    return saved


def enforce(store):
    store.update(SLUG, gates={"panic-call": "enforce"})


def stall_launches(store, since):
    spawn_stall.save(store, SLUG, {"since": since, "launch": 0, "at": since})


def items(store):
    return InboxStore(store.redis).inbox(f"master@{SLUG}")


def names(found):
    return [(signal.name, signal.since) for signal in found]


def test_no_signal_holds_on_a_healthy_swarm(store):
    assert panic_call.signals(store, SLUG, START, {}) == ([], {})


def test_each_degradation_signal_carries_its_measured_start(store):
    stall_launches(store, START)
    store.redis.set(
        store.key(SLUG, "merge-stall"), json.dumps({"repo": "o/r", "queue": 3, "merged": START - HOUR, "at": START})
    )
    blockers.record(store, SLUG, "TimeoutError", "agents: timed out", START - 2 * HOUR)
    blockers.record(store, SLUG, "TimeoutError", "agents: timed out", START)
    blockers.record(store, SLUG, "spawn stall", "No agent has launched", START)
    early_ends.note(store, SLUG, "doc")
    store.redis.set(
        store.key(SLUG, "urgent-hold"),
        json.dumps({"at": START, "tasks": {"t1": {"since": START - 3 * HOUR, "why": "", "title": "fix it"}}}),
    )
    found, kept = panic_call.signals(store, SLUG, START, {})
    assert names(found) == [
        ("No agent launch", START),
        ("No merge into dev", START - HOUR),
        ("Tick step failing with TimeoutError", START - 2 * HOUR),
        ("Tick passes ending early", START),
        ("Urgent task fix it held", START - 3 * HOUR),
    ]
    assert "3 queued entries on o/r" in found[1].evidence
    assert "2 failures" in found[2].evidence and "agents: timed out" in found[2].evidence
    assert "no step stored doc" in found[3].evidence
    assert found[4].evidence == "it is claimable but remains unclaimed"
    assert kept == {"value": "doc", "since": START}


def test_an_early_end_streak_keeps_its_first_start_until_the_missing_value_changes(store):
    early_ends.note(store, SLUG, "doc")
    early_ends.note(store, SLUG, "doc")
    found, kept = panic_call.signals(store, SLUG, START + HOUR, {"value": "doc", "since": START})
    assert (names(found), kept) == ([("Tick passes ending early", START)], {"value": "doc", "since": START})
    early_ends.note(store, SLUG, "view")
    found, kept = panic_call.signals(store, SLUG, START + HOUR, kept)
    assert kept == {"value": "view", "since": START + HOUR}


def test_a_merge_observation_error_or_an_empty_queue_is_no_merge_signal(store):
    key = store.key(SLUG, "merge-stall")
    store.redis.set(key, json.dumps({"repo": "o/r", "queue": 0, "merged": 0, "at": START}))
    assert panic_call.signals(store, SLUG, START, {})[0] == []
    store.redis.set(key, json.dumps({"repo": "o/r", "queue": 2, "merged": START, "at": START, "error": "gh down"}))
    assert panic_call.signals(store, SLUG, START, {})[0] == []
    store.redis.set(key, json.dumps({"repo": "o/r", "queue": 2, "at": START}))
    assert panic_call.signals(store, SLUG, START, {})[0] == []


def test_an_ended_early_streak_forgets_its_start(store):
    assert panic_call.signals(store, SLUG, START + HOUR, {"value": "doc", "since": START}) == ([], {})


def test_a_held_urgent_task_names_why_it_is_held(store):
    store.redis.set(
        store.key(SLUG, "urgent-hold"),
        json.dumps({"at": START, "tasks": {"t1": {"since": START, "why": "its claimant blocked it", "title": "x"}}}),
    )
    found = panic_call.signals(store, SLUG, START, {})[0]
    assert [(signal.name, signal.evidence) for signal in found] == [("Urgent task x held", "its claimant blocked it")]


def test_a_step_failure_counts_as_recurring_up_to_the_recent_window(store):
    blockers.record(store, SLUG, "KeyError", "doc", START - 2 * HOUR)
    blockers.record(store, SLUG, "KeyError", "doc", START)
    at = START + blockers.RECENT_MS
    assert names(panic_call.signals(store, SLUG, at, {})[0]) == [("Tick step failing with KeyError", START - 2 * HOUR)]
    assert panic_call.signals(store, SLUG, at + 1, {})[0] == []


def test_a_step_failure_that_stopped_recurring_is_no_signal(store):
    blockers.record(store, SLUG, "KeyError", "doc", START - 2 * HOUR)
    assert panic_call.signals(store, SLUG, START, {})[0] == []


def test_enforce_asks_the_master_at_the_first_held_hour_and_once_each_hour_after(store):
    enforce(store)
    stall_launches(store, START)
    assert panic_call.step(store, SLUG, START + HOUR - 1) == []
    assert items(store) == []
    lines = panic_call.step(store, SLUG, START + HOUR)
    assert len(lines) == 1 and lines[0].startswith(f"panic call: asked master@{SLUG}")
    assert panic_call.step(store, SLUG, START + HOUR + 30 * MINUTE) == []
    assert len(panic_call.step(store, SLUG, START + 2 * HOUR)) == 1
    asked = items(store)
    assert len(asked) == 2
    text = next(item.text for item in asked if "for 60 minutes" in item.text)
    assert text.startswith(f"Is this a panic mode call? No agent launch on swarm {SLUG} for 60 minutes")
    assert "a lane has a free seat, quota and claimable work" in text
    assert f"agentihooks swarm {SLUG} panic on --fix" in text
    assert f"agentihooks swarm {SLUG} merge priority" in text
    assert "say why on the ledger" in text
    assert asked[0].sender == "swarm"


def test_a_signal_first_seen_past_an_hour_asks_once_then_waits_for_the_next_hour(store):
    enforce(store)
    stall_launches(store, START)
    assert len(panic_call.step(store, SLUG, START + 3 * HOUR + MINUTE)) == 1
    assert panic_call.step(store, SLUG, START + 3 * HOUR + 50 * MINUTE) == []
    assert len(panic_call.step(store, SLUG, START + 4 * HOUR)) == 1


def test_a_new_start_asks_again_and_a_cleared_signal_is_forgotten(store):
    enforce(store)
    stall_launches(store, START)
    panic_call.step(store, SLUG, START + HOUR)
    spawn_stall.save(store, SLUG, {"at": START + HOUR})
    panic_call.step(store, SLUG, START + HOUR + MINUTE)
    assert store.redis.get(store.key(SLUG, panic_call.KEY)) is None
    stall_launches(store, START + 2 * HOUR)
    assert len(panic_call.step(store, SLUG, START + 3 * HOUR)) == 1
    assert len(items(store)) == 2


def test_observe_is_the_default_and_logs_the_question_without_sending_it(store):
    stall_launches(store, START)
    lines = panic_call.step(store, SLUG, START + HOUR)
    assert len(lines) == 1 and lines[0].startswith("panic call: logged Is this a panic mode call?")
    assert items(store) == []
    rows = gate_log.recent(SLUG)
    assert [(row["gate"], row["kind"], row["agent"]) for row in rows] == [("panic-call", "observe", "swarm")]
    assert rows[0]["reason"].startswith("Is this a panic mode call? No agent launch")
    assert rows[0]["at"] == START + HOUR


def test_an_early_end_streak_start_carries_across_passes_to_the_held_hour(store):
    enforce(store)
    early_ends.note(store, SLUG, "doc")
    assert panic_call.step(store, SLUG, START) == []
    assert panic_call.read(store, SLUG)["early"] == {"value": "doc", "since": START}
    lines = panic_call.step(store, SLUG, START + HOUR)
    assert len(lines) == 1 and "Tick passes ending early on swarm sw for 60 minutes" in lines[0]


def test_a_signal_not_yet_held_an_hour_does_not_stop_a_later_one_being_asked(store):
    enforce(store)
    stall_launches(store, START + 2 * HOUR)
    store.redis.set(
        store.key(SLUG, "merge-stall"), json.dumps({"repo": "o/r", "queue": 1, "merged": START, "at": START})
    )
    lines = panic_call.step(store, SLUG, START + 2 * HOUR)
    assert len(lines) == 1 and "No merge into dev on swarm sw for 120 minutes" in lines[0]


def test_off_skips_without_reading_or_writing_state(store):
    store.update(SLUG, gates={"panic-call": "off"})
    stall_launches(store, START)
    assert panic_call.step(store, SLUG, START + HOUR) == []
    assert store.redis.get(store.key(SLUG, panic_call.KEY)) is None
    assert gate_log.recent(SLUG) == []


def test_a_repeated_send_of_the_same_hour_is_one_item(store):
    enforce(store)
    stall_launches(store, START)
    panic_call.step(store, SLUG, START + HOUR)
    store.redis.delete(store.key(SLUG, panic_call.KEY))
    lines = panic_call.step(store, SLUG, START + HOUR + MINUTE)
    assert len(lines) == 1 and lines[0].startswith(f"panic call: already asked master@{SLUG}")
    assert len(items(store)) == 1


def test_the_question_names_its_utc_start_and_the_item_carries_its_hour_ref(store, monkeypatch):
    import hashlib
    import time

    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    try:
        enforce(store)
        stall_launches(store, START)
        panic_call.step(store, SLUG, START + HOUR)
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
    (item,) = items(store)
    assert "for 60 minutes, since 15 January 08:00 UTC. Evidence: " in item.text
    ref = f"panic-call:{SLUG}:No agent launch|{START}:1"
    assert (item.ref, item.id) == (ref, hashlib.sha256(ref.encode()).hexdigest()[:12])


def test_the_spawn_stall_watch_runs_the_panic_call_after_each_pass(store, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(spawn_stall, "observe", lambda *args: {})
    monkeypatch.setattr(spawn_stall, "deliver", lambda *args: None)
    monkeypatch.setattr(spawn_stall.panic, "tick_pass", lambda *args: [])
    monkeypatch.setattr(panic_call, "step", lambda store, slug, at: seen.append((slug, at)) or ["panic call: asked"])
    with spawn_stall.watch(store, SLUG, None, lambda: START, None):
        pass
    assert seen == [(SLUG, START)]
    event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert event == {"event": "swarm_panic_call", "slug": SLUG, "action": "panic call: asked"}


def test_a_planted_two_hour_no_launch_window_sends_the_master_the_panic_question(store, monkeypatch):
    enforce(store)
    stall_launches(store, START - 2 * HOUR)
    monkeypatch.setattr(spawn_stall, "eligible", lambda *args: True)
    monkeypatch.setattr(spawn_stall, "launched", lambda store, slug: 0)
    with spawn_stall.watch(store, SLUG, None, lambda: START, None):
        pass
    asked = [item for item in items(store) if item.text.startswith("Is this a panic mode call?")]
    assert len(asked) == 1
    assert f"No agent launch on swarm {SLUG} for 120 minutes" in asked[0].text
    assert spawn_stall.read(store, SLUG)["since"] == START - 2 * HOUR


ENGINEER = "engineer@1-1"


def claimed_task(task_id: str, claimed_by: str, **fields: Any) -> dict[str, Any]:
    return {
        "id": task_id,
        "phase": "p1",
        "lane": "eng",
        "rank": "urgent",
        "state": "claimed",
        "claimed_by": claimed_by,
        **fields,
    }


def observe_tasks(store: Any, at: int, *tasks: dict[str, Any]) -> None:
    from scripts.swarm.health import urgent_hold

    found = {"overview": "o", "phases": [{"id": "p1", "title": "build", "depends_on": []}], "tasks": list(tasks)}
    urgent_hold.observe(store, SLUG, found, at)


def urgent_claim(store: Any, at: int, claimed_by: str = ENGINEER) -> None:
    observe_tasks(store, at, claimed_task("t9", claimed_by, title="fix the outage"))


def claimant(store: Any, started_at: int, lane: str = "eng", name: str = ENGINEER, task: str = "t9") -> None:
    from scripts.swarm.store import AgentRecord

    store.put_agent(SLUG, AgentRecord(name, lane, task, started_at=started_at))


def test_urgent_hold_records_only_urgent_claimed_tasks_with_their_claimant(store: Any) -> None:
    from scripts.swarm.health import urgent_hold

    observe_tasks(
        store,
        START,
        claimed_task("a", ENGINEER, title="hot"),
        claimed_task("b", "x", title="calm", rank=""),
        claimed_task("c", "y", title="review", state="pr"),
        claimed_task("d", "z", title="gone", out_of_scope=True),
        {"id": "e", "phase": "p1", "lane": "eng", "rank": "urgent", "state": "claimed"},
    )
    assert urgent_hold.read(store, SLUG)["claimed"] == {
        "a": {"title": "hot", "claimed_by": ENGINEER, "state": "claimed"},
        "e": {"title": "e", "claimed_by": "", "state": "claimed"},
    }


def test_no_urgent_claim_leaves_the_stored_hold_without_a_claimed_key(store: Any) -> None:
    from scripts.swarm.health import urgent_hold

    observe_tasks(store, START)
    assert urgent_hold.read(store, SLUG) == {"at": START, "tasks": {}}


def test_an_urgent_claim_quiet_past_the_stale_window_is_a_signal_from_its_last_progress(store: Any) -> None:
    urgent_claim(store, START)
    claimant(store, START - 30 * MINUTE)
    found = panic_call.signals(store, SLUG, START, {})[0]
    assert [(signal.name, signal.since, signal.after) for signal in found] == [
        ("Urgent task fix the outage claim quiet", START - 30 * MINUTE, 30 * MINUTE)
    ]
    assert found[0].evidence == f"claimed by {ENGINEER} with no progress for 30 minutes"


def test_quiet_urgent_claims_are_signals_in_claimant_name_order(store: Any) -> None:
    observe_tasks(
        store,
        START,
        claimed_task("t2", "engineer@1-2", title="second"),
        claimed_task("t1", "engineer@1-1", title="first"),
    )
    claimant(store, START - HOUR, name="engineer@1-2", task="t2")
    claimant(store, START - 2 * HOUR, name="engineer@1-1", task="t1")
    found = panic_call.signals(store, SLUG, START, {})[0]
    assert [signal.name for signal in found] == ["Urgent task first claim quiet", "Urgent task second claim quiet"]


def test_an_urgent_claim_inside_the_stale_window_or_with_a_declared_wait_is_no_signal(store: Any) -> None:
    from scripts.swarm import idle

    urgent_claim(store, START)
    claimant(store, START - 30 * MINUTE + 1)
    assert panic_call.signals(store, SLUG, START, {})[0] == []
    claimant(store, START - 2 * HOUR)
    idle.declare_wait(store.redis, SLUG, ENGINEER, START + HOUR, "checks on the pull request", START)
    assert panic_call.signals(store, SLUG, START, {})[0] == []


def test_a_quiet_claim_by_another_agent_or_lane_is_no_signal(store: Any) -> None:
    urgent_claim(store, START, claimed_by="engineer@1-2")
    claimant(store, START - 2 * HOUR)
    assert panic_call.signals(store, SLUG, START, {})[0] == []
    urgent_claim(store, START)
    claimant(store, START - 2 * HOUR, lane="master")
    assert panic_call.signals(store, SLUG, START, {})[0] == []
    claimant(store, 0)
    assert panic_call.signals(store, SLUG, START, {})[0] == []


def test_the_stale_window_follows_the_health_setting(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIHOOKS_HEALTH_STALE_MINUTES", "50")
    urgent_claim(store, START)
    claimant(store, START - 45 * MINUTE)
    assert panic_call.signals(store, SLUG, START, {})[0] == []
    found = panic_call.signals(store, SLUG, START + 5 * MINUTE, {})[0]
    assert [signal.after for signal in found] == [50 * MINUTE]


def test_a_quiet_urgent_claim_is_asked_at_the_stale_window_and_once_each_hour_after(store: Any) -> None:
    enforce(store)
    urgent_claim(store, START)
    claimant(store, START)
    assert panic_call.step(store, SLUG, START + 30 * MINUTE - 1) == []
    assert len(panic_call.step(store, SLUG, START + 30 * MINUTE)) == 1
    assert panic_call.step(store, SLUG, START + 89 * MINUTE) == []
    assert len(panic_call.step(store, SLUG, START + 90 * MINUTE)) == 1
    assert len(items(store)) == 2


def test_a_planted_quiet_urgent_claim_sends_the_master_one_panic_question_in_one_watch_pass(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    enforce(store)
    urgent_claim(store, START)
    claimant(store, START - 45 * MINUTE)
    monkeypatch.setattr(spawn_stall, "eligible", lambda *args: False)
    with spawn_stall.watch(store, SLUG, None, lambda: START, None):
        pass
    asked = [item for item in items(store) if item.text.startswith("Is this a panic mode call?")]
    assert len(asked) == 1
    assert f"Urgent task fix the outage claim quiet on swarm {SLUG} for 45 minutes" in asked[0].text
    assert f"claimed by {ENGINEER} with no progress for 45 minutes" in asked[0].text
    assert asked[0].ref == f"panic-call:{SLUG}:Urgent task fix the outage claim quiet|{START - 45 * MINUTE}:1"
