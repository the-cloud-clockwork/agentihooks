import pytest

from scripts.hive import admission, placement, placement_waits
from scripts.inbox import store as inbox_store
from scripts.inbox.store import InboxStore
from scripts.swarm import capacity, cli, commands, lease, local_slots, status, tick
from scripts.swarm.health import spawn_stall
from scripts.swarm.store import AgentRecord, SwarmConfig
from tests.swarm.test_cli import env as env
from tests.swarm.test_delivery import FakeHerdr

pytestmark = pytest.mark.unit


@pytest.fixture
def stalled(env, monkeypatch):
    store, ledger, runtime = env
    store.create(SwarmConfig("sw", "/repo", 1, 0, max_plan=0, scaling="manual"))
    boss = AgentRecord("boss", "master", "master", state="working", seat="master@sw")
    store.put_agent("sw", boss)
    store.seats.occupy(boss.seat, boss.name, 1)
    runtime.live.add(boss.name)
    account = capacity.Account("claude", "acct", "OPEN", 1, 80, 80, 6)
    monkeypatch.setattr(capacity, "accounts", lambda *args, **kwargs: [account])
    original_state = ledger.state

    def state(slug):
        assert slug == "sw"
        return original_state(slug)

    ledger.state = state
    clock = [10_000_000]
    monkeypatch.setattr(cli, "now_ms", lambda: clock[0])
    monkeypatch.setattr(inbox_store, "now_ms", lambda: clock[0])
    monkeypatch.setattr(status, "now_ms", lambda: clock[0])
    monkeypatch.setattr(local_slots, "live", set)
    error = RuntimeError("forced phase failure")

    def broken(*args):
        raise error

    monkeypatch.setattr(cli.phases, "phase_pass", broken)
    return store, ledger, runtime, clock, error


def tick_failure(stalled):
    store, ledger, runtime, _, error = stalled
    with pytest.raises(RuntimeError) as caught:
        cli.run_tick(store, "sw", ledger, runtime, FakeHerdr({}))
    assert caught.value is error
    assert not store.redis.exists(store.key("sw", "tick-lock"))


def alarms(stalled):
    store, ledger, _, _, _ = stalled
    return [
        row
        for row in status.findings(store, "sw", store.config("sw"), ledger.tasks("sw"), [])
        if row["kind"] == "spawn stall"
    ]


def test_repeated_failed_tick_alarms_at_ten_minutes_and_names_the_step(stalled):
    store, _, _, clock, _ = stalled
    tick_failure(stalled)
    assert alarms(stalled) == []
    clock[0] += 599_999
    tick_failure(stalled)
    assert alarms(stalled) == []
    clock[0] += 1
    tick_failure(stalled)
    (finding,) = alarms(stalled)
    assert finding["id"] == "spawn-stall/sw"
    assert finding["summary"] == "No agent has launched for ten minutes"
    assert finding["threshold"] == "ten minutes"
    assert finding["evidence"][0] == "A lane has a free seat, quota and claimable work"
    assert spawn_stall.findings(store, "sw")[0].measure == 1
    assert any("broken" in line and "forced phase failure" in line for line in finding["evidence"])
    mail = InboxStore(store.redis).pending_mail("master@sw")
    assert len(mail) == 1
    assert mail[0].sender == "swarm"
    assert mail[0].text == (
        "Spawn stall on sw: No agent has launched for ten minutes. "
        "A lane has a free seat, quota and claimable work; "
        "Last failed step tests.swarm.test_spawn_stall.stalled.<locals>.broken: RuntimeError: forced phase failure"
    )
    tick_failure(stalled)
    assert len(InboxStore(store.redis).pending_mail("master@sw")) == 1


def test_failed_ticks_publish_the_spawn_stall_alarm_for_the_page(stalled):
    store, _, _, clock, _ = stalled
    tick_failure(stalled)
    assert commands.view(store, "sw").get("findings") == []
    clock[0] += 600_000
    tick_failure(stalled)
    (finding,) = commands.view(store, "sw")["findings"]
    assert finding["id"] == "spawn-stall/sw"
    assert finding["kind"] == "spawn stall"
    assert finding["summary"] == "No agent has launched for ten minutes"
    assert finding["evidence"] == [
        "A lane has a free seat, quota and claimable work",
        "Last failed step tests.swarm.test_spawn_stall.stalled.<locals>.broken: RuntimeError: forced phase failure",
    ]


@pytest.mark.parametrize("failed_tick", [True, False])
def test_publication_failure_preserves_the_tick_error_and_releases_the_lock(stalled, monkeypatch, failed_tick):
    from scripts.swarm import command_runner, timing

    store, ledger, runtime, _, tick_error = stalled
    publication_error = RuntimeError("forced publication failure")

    def broken_publish(*args):
        raise publication_error

    monkeypatch.setattr(command_runner, "publish", broken_publish)
    if not failed_tick:
        monkeypatch.setattr(cli.phases, "phase_pass", lambda *args: [])
    keeping = timing.BEFORE_STEP.get()
    with pytest.raises(RuntimeError) as caught:
        cli.run_tick(store, "sw", ledger, runtime, FakeHerdr({}))
    assert caught.value is (tick_error if failed_tick else publication_error)
    assert not store.redis.exists(store.key("sw", "tick-lock"))
    assert timing.BEFORE_STEP.get() is keeping


def test_unanswered_alarm_notifies_the_operator_after_ten_further_minutes(stalled):
    store, ledger, _, clock, _ = stalled
    tick_failure(stalled)
    clock[0] += 600_000
    tick_failure(stalled)
    inbox = InboxStore(store.redis)
    (item,) = inbox.pending_mail("master@sw")
    inbox.read(item.id, "boss")
    clock[0] += 599_999
    tick_failure(stalled)
    assert ledger.notes == []
    clock[0] += 1
    tick_failure(stalled)
    assert len(ledger.notes) == 1
    assert "forced phase failure" in ledger.notes[0]
    tick_failure(stalled)
    assert len(ledger.notes) == 1


def test_answered_alarm_never_notifies_the_operator(stalled):
    store, ledger, _, clock, _ = stalled
    tick_failure(stalled)
    clock[0] += 600_000
    tick_failure(stalled)
    inbox = InboxStore(store.redis)
    (item,) = inbox.pending_mail("master@sw")
    inbox.close(item.id, "boss", "done", "handled")
    clock[0] += 600_000
    tick_failure(stalled)
    assert ledger.notes == []


def test_generic_wake_cannot_escalate_the_dedicated_alarm(stalled):
    from scripts.inbox import wake

    store, _, _, clock, _ = stalled
    tick_failure(stalled)
    clock[0] += 600_000
    tick_failure(stalled)
    inbox = InboxStore(store.redis)
    (item,) = inbox.pending_mail("master@sw")
    assert wake.decide(item, None, inbox.history(item.id), clock[0] + 300_000, 300_000, "sw") is None


def test_normal_findings_pass_sends_no_duplicate_alarm(stalled):
    from scripts.swarm import ledger_events

    store, _, _, clock, _ = stalled
    tick_failure(stalled)
    clock[0] += 600_000
    tick_failure(stalled)
    inbox = InboxStore(store.redis)
    other = {
        "id": "idle/worker",
        "kind": "idle",
        "subject": "worker",
        "summary": "A worker is idle",
        "verdict": None,
    }
    sent = ledger_events.findings_pass(inbox, store, "sw", alarms(stalled) + [other])
    assert sent == ["told master@sw: finding:idle/worker:0"]
    assert len(inbox.pending_mail("master@sw")) == 2


@pytest.mark.parametrize("unavailable", ["paused", "stopped", "seat", "quota", "harness", "task"])
def test_unavailable_launch_conditions_reset_the_alarm(stalled, monkeypatch, unavailable):
    store, ledger, runtime, clock, _ = stalled
    tick_failure(stalled)
    clock[0] += 600_000
    if unavailable in ("paused", "stopped"):
        store.update("sw", state=unavailable)
    elif unavailable == "seat":
        store.update("sw", max_eng=0)
    elif unavailable == "quota":
        monkeypatch.setattr(capacity, "accounts", lambda *args, **kwargs: [])
    elif unavailable == "harness":
        store.update("sw", lanes={"eng": {"agent": "codex"}})
    else:
        ledger.rows["t1"]["state"] = "blocked"
    if unavailable == "stopped":
        assert cli.run_tick(store, "sw", ledger, runtime, FakeHerdr({})) == []
    else:
        tick_failure(stalled)
    assert alarms(stalled) == []
    assert InboxStore(store.redis).pending_mail("master@sw") == []


def test_a_successful_launch_restarts_the_clock(stalled):
    store, _, _, clock, _ = stalled
    tick_failure(stalled)
    clock[0] += 500_000
    store.record_launch("sw", AgentRecord("finished", "eng", "t1", started_at=clock[0]), "started")
    tick_failure(stalled)
    clock[0] += 599_999
    tick_failure(stalled)
    assert alarms(stalled) == []
    clock[0] += 1
    tick_failure(stalled)
    assert len(alarms(stalled)) == 1


def test_a_failure_from_before_the_stall_is_not_named_as_its_cause(stalled):
    store, ledger, runtime, clock, _ = stalled
    tick_failure(stalled)
    stored = store.redis.get(store.key("sw", "tick-failure"))
    assert '"at": 10000000' in stored
    clock[0] += 1
    store.record_launch("sw", AgentRecord("finished", "eng", "t1", started_at=clock[0]), "started")
    for _ in range(2):
        with spawn_stall.watch(store, "sw", ledger, lambda: clock[0], runtime):
            pass
        clock[0] += 600_000
    (finding,) = spawn_stall.findings(store, "sw")
    assert finding.evidence == ("A lane has a free seat, quota and claimable work",)
    tick_failure(stalled)
    (finding,) = spawn_stall.findings(store, "sw")
    assert finding.evidence[-1].startswith("Last failed step ")


def test_a_failure_recorded_at_the_start_of_the_stall_is_named(stalled):
    store, _, _, clock, _ = stalled
    tick_failure(stalled)
    clock[0] += 600_000
    with spawn_stall.watch(store, "sw", stalled[1], lambda: clock[0], stalled[2]):
        pass
    (finding,) = spawn_stall.findings(store, "sw")
    assert finding.evidence[-1] == (
        "Last failed step tests.swarm.test_spawn_stall.stalled.<locals>.broken: RuntimeError: forced phase failure"
    )


def test_a_stored_failure_without_a_time_is_not_named(stalled):
    store, ledger, runtime, clock, _ = stalled
    for _ in range(2):
        with spawn_stall.watch(store, "sw", ledger, lambda: clock[0], runtime):
            pass
        store.redis.set(store.key("sw", "tick-failure"), '{"step": "old", "error": "RuntimeError: old"}')
        clock[0] += 600_000
    (finding,) = spawn_stall.findings(store, "sw")
    assert finding.evidence == ("A lane has a free seat, quota and claimable work",)


def test_failure_callback_is_restored_and_outer_steps_preserve_the_cause(stalled):
    from scripts.swarm import timing

    store, ledger, _, clock, _ = stalled
    error = RuntimeError("inner failure")

    def broken():
        raise error

    def outer():
        timing.call(broken)

    with timing.tick("sw"), pytest.raises(RuntimeError) as caught:
        with spawn_stall.watch(store, "sw", ledger, lambda: clock[0], stalled[2]):
            timing.call(outer)
    assert caught.value is error
    assert timing.ON_FAILURE.get() is None
    clock[0] += 600_000
    tick_failure(stalled)
    assert "broken" in spawn_stall.findings(store, "sw")[0].evidence[-1]


def test_an_early_tick_failure_still_raises_the_alarm(stalled, monkeypatch):
    from scripts.swarm import command_runner

    _, _, _, clock, error = stalled

    def broken(*args):
        raise error

    monkeypatch.setattr(command_runner, "consume", broken)
    tick_failure(stalled)
    clock[0] += 600_000
    tick_failure(stalled)
    assert len(alarms(stalled)) == 1


def test_caught_spawn_failures_keep_the_error_without_resetting_the_clock(stalled):
    from scripts.swarm import tick, timing

    store, ledger, _, clock, _ = stalled
    record = AgentRecord("failed", "eng", "t1", started_at=clock[0])
    with timing.tick("sw"), spawn_stall.watch(store, "sw", ledger, lambda: clock[0], stalled[2]):
        tick._record_spawn_failure("sw", store, record, RuntimeError("forced launch failure"), clock[0])
    clock[0] += 600_000
    with timing.tick("sw"), spawn_stall.watch(store, "sw", ledger, lambda: clock[0], stalled[2]):
        pass
    (finding,) = spawn_stall.findings(store, "sw")
    assert finding.evidence[-1] == "Last failed step scripts.swarm.tick._spawn: RuntimeError: forced launch failure"


def test_eligibility_return_starts_a_fresh_ten_minute_interval(stalled):
    store, ledger, _, clock, _ = stalled
    tick_failure(stalled)
    clock[0] += 500_000
    ledger.rows["t1"]["state"] = "blocked"
    tick_failure(stalled)
    clock[0] += 600_000
    ledger.rows["t1"]["state"] = "open"
    tick_failure(stalled)
    assert alarms(stalled) == []
    clock[0] += 599_999
    tick_failure(stalled)
    assert alarms(stalled) == []
    clock[0] += 1
    tick_failure(stalled)
    assert len(alarms(stalled)) == 1


def test_task_specific_harness_restrictions_require_compatible_quota(stalled, monkeypatch):
    from scripts.swarm.runtime import HerdrRuntime, plugins

    store, ledger, runtime, clock, _ = stalled
    ledger.rows["t1"]["profile"] = "frontend"
    runtime.quota_requirements = HerdrRuntime().quota_requirements
    monkeypatch.setattr(plugins, "claude_only", lambda profile: profile == "frontend")
    account = capacity.Account("codex", "acct", "OPEN", 0, 80, 80, 6)
    monkeypatch.setattr(capacity, "accounts", lambda *args, **kwargs: [account])
    tick_failure(stalled)
    clock[0] += 600_000
    tick_failure(stalled)
    assert alarms(stalled) == []
    assert InboxStore(store.redis).pending_mail("master@sw") == []


def test_lost_lease_prevents_failure_and_alarm_writes(stalled):
    from scripts.swarm import timing
    from scripts.swarm.store import SwarmError

    store, ledger, _, clock, _ = stalled
    tick_failure(stalled)
    before = store.redis.get(store.key("sw", "tick-failure"))
    error = SwarmError("the controller lease was lost")

    def lost():
        raise error

    with timing.tick("sw"), pytest.raises(SwarmError) as caught:
        with spawn_stall.watch(store, "sw", ledger, lambda: clock[0], stalled[2]):
            token = timing.BEFORE_STEP.set(lost)
            try:
                with timing.step("forced lost lease"):
                    raise error
            finally:
                timing.BEFORE_STEP.reset(token)
    assert caught.value is error
    assert store.redis.get(store.key("sw", "tick-failure")) == before


def test_quota_handoff_requires_an_account_other_than_its_predecessor(stalled, monkeypatch):
    from scripts.swarm.runtime import HerdrRuntime, plugins

    store, _, runtime, clock, _ = stalled
    reader = HerdrRuntime()
    runtime.quota_requirements = reader.quota_requirements
    runtime.quota_capacity = reader.quota_capacity
    runtime.quota_previous = reader.quota_previous
    monkeypatch.setattr(plugins, "claude_only", lambda profile: False)
    monkeypatch.setattr(
        store,
        "handoff_envelope",
        lambda slug, task: {"reason": "quota", "launch": {"harness": "claude", "account": "acct"}},
    )
    tick_failure(stalled)
    clock[0] += 600_000
    tick_failure(stalled)
    assert alarms(stalled) == []
    assert InboxStore(store.redis).pending_mail("master@sw") == []


def test_lost_controller_lease_blocks_final_alarm_delivery(stalled, monkeypatch):
    from scripts.swarm import lease
    from scripts.swarm.store import SwarmError

    store, ledger, _, clock, _ = stalled
    tick_failure(stalled)
    before = store.redis.get(store.key("sw", "spawn-stall"))
    failure = store.redis.get(store.key("sw", "tick-failure"))
    clock[0] += 600_000

    def takeover(*args):
        store.redis.delete(store.key("sw", "control-owner"))
        lease.acquire(store, "sw", "replacement")
        raise RuntimeError("old tick failed after takeover")

    monkeypatch.setattr(cli.phases, "phase_pass", takeover)
    with pytest.raises(SwarmError):
        cli.run_tick(store, "sw", ledger, stalled[2], FakeHerdr({}))
    assert store.redis.get(store.key("sw", "spawn-stall")) == before
    assert store.redis.get(store.key("sw", "tick-failure")) == failure
    assert InboxStore(store.redis).pending_mail("master@sw") == []
    assert ledger.notes == []


def test_capacity_reader_receives_demand_clock_and_previous_state_without_refresh(stalled):
    import json

    store, ledger, runtime, clock, _ = stalled
    previous = {"at": 123, "effective": {"eng": 1, "ci": 0, "plan": 0}}
    store.redis.set(store.key("sw", "quota-capacity"), json.dumps(previous))
    received = []
    runtime.quota_previous = received.append

    def reader(config, agents, now, demand, requirements, *, refresh):
        assert config.slug == "sw"
        assert [agent.name for agent in agents] == ["boss"]
        assert now == 10_000
        assert demand == {"eng": 1, "ci": 1, "plan": 0}
        assert requirements is None
        assert refresh is False
        return {"placements": {"eng": [{"harness": "claude", "account": "acct"}]}}

    runtime.quota_capacity = reader
    counters = []
    runtime.quota_spent = counters.append
    store.redis.zadd(tick.HOST_SPENDS, {"now": clock[0], "later": clock[0] + 1})
    assert spawn_stall.eligible(store, "sw", ledger, clock[0], runtime)
    assert received == [previous]
    (counter,) = counters
    assert counter(clock[0]) == 1


def test_a_missing_ledger_has_no_claimable_work(stalled, monkeypatch):
    from scripts.swarm.ledger_client import LedgerGone

    store, ledger, runtime, clock, _ = stalled

    def missing(slug):
        raise LedgerGone("no such scratch ledger")

    monkeypatch.setattr(ledger, "state", missing)
    assert not spawn_stall.eligible(store, "sw", ledger, clock[0], runtime)


def test_a_busy_lane_has_no_free_seat(stalled):
    store, ledger, runtime, clock, _ = stalled
    ledger.rows["t2"].update(state="claimed", claimed_by="worker")
    store.put_agent("sw", AgentRecord("worker", "eng", "t2", state="working"))
    assert not spawn_stall.eligible(store, "sw", ledger, clock[0], runtime)


@pytest.mark.parametrize("state", ["starting", "working"])
def test_a_successful_master_launch_restarts_the_interval(stalled, state):
    store, _, _, clock, _ = stalled
    tick_failure(stalled)
    clock[0] += 500_000
    store.put_agent("sw", AgentRecord("boss", "master", "master", started_at=clock[0], state=state, seat="master@sw"))
    tick_failure(stalled)
    clock[0] += 599_999
    tick_failure(stalled)
    assert alarms(stalled) == []
    clock[0] += 1
    tick_failure(stalled)
    assert len(alarms(stalled)) == 1


def test_no_launch_is_reported_until_an_agent_actually_starts(stalled):
    store, _, _, clock, _ = stalled
    store.drop_agent("sw", "boss")
    assert spawn_stall.launched(store, "sw") == 0
    store.record_launch("sw", AgentRecord("pending", "eng", "t1", started_at=clock[0]), "pending")
    store.put_agent("sw", AgentRecord("finished", "eng", "t1", started_at=clock[0], state="finished"))
    assert spawn_stall.launched(store, "sw") == 0


def test_a_stall_without_a_recorded_error_still_has_eligibility_evidence(stalled):
    store, ledger, runtime, clock, _ = stalled
    with spawn_stall.watch(store, "sw", ledger, lambda: clock[0], runtime):
        pass
    clock[0] += 600_000
    with spawn_stall.watch(store, "sw", ledger, lambda: clock[0], runtime):
        pass
    (finding,) = spawn_stall.findings(store, "sw")
    assert finding.evidence == ("A lane has a free seat, quota and claimable work",)


def test_a_quota_warning_hold_is_not_available_capacity(stalled, monkeypatch):
    store, ledger, runtime, clock, _ = stalled
    account = capacity.Account("claude", "acct", "OPEN", 0, 80, 9, 6)
    monkeypatch.setattr(capacity, "accounts", lambda *args, **kwargs: [account])
    assert not spawn_stall.eligible(store, "sw", ledger, clock[0], runtime)


def test_autoscaling_with_no_host_room_offers_no_launch_seat(stalled, monkeypatch):
    from scripts.swarm import host_budget

    store, ledger, runtime, clock, _ = stalled
    store.update("sw", scaling="auto")
    monkeypatch.setattr(host_budget, "read_host", lambda: host_budget.HostSample(100, 1, 0, 0))
    assert not spawn_stall.eligible(store, "sw", ledger, clock[0], runtime)


def test_a_manual_swarm_held_by_the_host_is_not_a_spawn_stall(stalled, monkeypatch):
    from scripts.swarm import host_budget

    store, ledger, runtime, clock, _ = stalled
    store.update("sw", scaling="manual")
    assert spawn_stall.eligible(store, "sw", ledger, clock[0], runtime)
    monkeypatch.setattr(host_budget, "read_host", lambda: host_budget.HostSample(100, 1, 0, 0))
    assert not spawn_stall.eligible(store, "sw", ledger, clock[0], runtime)


def test_an_auto_swarm_held_between_the_watermarks_scales_from_its_first_grant(stalled, monkeypatch):
    import json

    from scripts.swarm import host_budget

    store, ledger, runtime, clock, _ = stalled
    store.update("sw", scaling="auto")
    granted = {"room": 2, "reason": "r", "limit": "load", "held": False, "granted_at": clock[0] - 60_000}
    store.redis.set(store.key("sw", "quota-capacity"), json.dumps({"host": granted}))
    store.redis.zadd(tick.HOST_SPENDS, {"first": clock[0] - 59_000})
    monkeypatch.setattr(host_budget, "read_host", lambda: host_budget.HostSample(10.0, 8, 64_000, 2))
    seen = []
    autoscaled = capacity.autoscaled
    monkeypatch.setattr(
        capacity,
        "autoscaled",
        lambda config, inputs, host: (
            seen.append((host["granted_at"], capacity.unspent(host, inputs.spent))) or autoscaled(config, inputs, host)
        ),
    )
    spawn_stall.eligible(store, "sw", ledger, clock[0], runtime)
    assert seen == [(clock[0] - 60_000, 1)]


def test_a_doctor_held_by_the_watched_focus_is_not_a_spawn_stall(stalled, monkeypatch):
    from scripts.doctor import priming

    store, ledger, runtime, clock, _ = stalled
    store.update("sw", template=priming.TEMPLATE)
    store.set_peer("sw", "watched")
    own, watched = ledger.state, {"tasks": [], "freezes": []}
    monkeypatch.setattr(ledger, "state", lambda slug: own(slug) if slug == "sw" else watched)
    assert spawn_stall.eligible(store, "sw", ledger, clock[0], runtime)
    watched["freezes"] = [{"verb": "focus", "target": "lane:ci"}]
    assert not spawn_stall.eligible(store, "sw", ledger, clock[0], runtime)


HIVE_FULL = "no hive for eng has room"
CPU_GATE = {"id": "anton-1", "refusal": admission.refusal(admission.Room(0, admission.CPU, "five minute load 9.00"))}
MAX_GATE = {"id": "anton-2", "refusal": admission.refusal(admission.capped(admission.Room(1, "", ""), 3, 3))}
CPU_WAIT = placement._refused(HIVE_FULL, [CPU_GATE])
FULL_WAIT = placement.overflow(
    "engineer", placement.Wait(placement._refused(HIVE_FULL, [CPU_GATE, MAX_GATE])), 8, 8
).reason
REFUSALS = (
    "Admission refusals, latest first: local cap on ws (8 of 8 agents), cpu on anton-1 (2 tasks), "
    "max agents on anton-2 (1 task)"
)


def seed_refusals(store, clock):
    placement_waits.observe(store, "sw", "eng", "t1", CPU_WAIT, clock[0] + 1)
    placement_waits.observe(store, "sw", "ci", "t2", FULL_WAIT, clock[0] + 2)
    spawn_stall.refuse_local(store, "sw", "ws", 8, 8, clock[0] + 3)


def quiet_watch(stalled):
    store, ledger, runtime, clock, _ = stalled
    with spawn_stall.watch(store, "sw", ledger, lambda: clock[0], runtime):
        pass


@pytest.mark.parametrize("reason", ["Codex hive placement is not enabled", CPU_WAIT])
def test_current_placement_holds_explain_the_spawn_wait(stalled, reason):
    store, ledger, _, clock, _ = stalled
    for _ in range(2):
        for task in ledger.rows.values():
            placement_waits.observe(store, "sw", task["lane"], task["id"], reason, clock[0])
        quiet_watch(stalled)
        clock[0] += 600_000
    assert spawn_stall.findings(store, "sw") == []
    assert InboxStore(store.redis).pending_mail("master@sw") == []


def test_a_repeated_hold_uses_its_latest_observation(stalled):
    store, _, _, clock, _ = stalled
    placement_waits.observe(store, "sw", "eng", "t1", CPU_WAIT, clock[0] - 1)
    for _ in range(2):
        placement_waits.observe(store, "sw", "eng", "t1", CPU_WAIT, clock[0])
        quiet_watch(stalled)
        clock[0] += 600_000
    assert spawn_stall.findings(store, "sw") == []


def test_a_legacy_hold_without_seen_at_uses_its_start(stalled):
    import json

    store, _, _, clock, _ = stalled
    row = {"lane": "eng", "reason": CPU_WAIT, "started_at": clock[0]}
    store.redis.hset(store.key("sw", placement_waits.WAITS), "t1", json.dumps(row))
    quiet_watch(stalled)
    assert "since" not in spawn_stall.read(store, "sw")


@pytest.mark.parametrize("lane", ["eng", "ci"])
def test_an_unheld_task_keeps_the_stall_alarm_eligible(stalled, lane):
    store, ledger, _, clock, _ = stalled
    store.update("sw", max_ci=1)
    ledger.rows["t3"] = {**ledger.rows["t1"], "id": "t3", "lane": lane}
    for _ in range(2):
        for task in ("t1", "t2"):
            row = ledger.rows[task]
            placement_waits.observe(store, "sw", row["lane"], task, CPU_WAIT, clock[0])
        quiet_watch(stalled)
        clock[0] += 600_000
    assert len(spawn_stall.findings(store, "sw")) == 1


def test_an_expired_placement_hold_starts_a_new_stall_interval(stalled):
    store, _, _, clock, _ = stalled
    placement_waits.observe(store, "sw", "eng", "t1", CPU_WAIT, clock[0])
    quiet_watch(stalled)
    clock[0] += 1
    quiet_watch(stalled)
    clock[0] += 1
    quiet_watch(stalled)
    clock[0] += 599_999
    quiet_watch(stalled)
    assert spawn_stall.findings(store, "sw") == []
    clock[0] += 1
    quiet_watch(stalled)
    assert len(spawn_stall.findings(store, "sw")) == 1


def test_a_wait_recorded_for_another_lane_does_not_hide_ready_work(stalled):
    store, _, _, clock, _ = stalled
    for _ in range(2):
        placement_waits.observe(store, "sw", "ci", "t1", CPU_WAIT, clock[0])
        quiet_watch(stalled)
        clock[0] += 600_000
    assert len(spawn_stall.findings(store, "sw")) == 1


def capped_watch(stalled, refused=True):
    store, ledger, runtime, clock, _ = stalled
    with spawn_stall.watch(store, "sw", ledger, lambda: clock[0], runtime):
        if refused:
            spawn_stall.refuse_local(store, "sw", "ws", 8, 8, clock[0])


@pytest.mark.parametrize("workers, raised", [(8, False), (7, True)])
def test_a_full_local_cap_holds_the_lane_without_a_spawn_stall(stalled, monkeypatch, workers, raised):
    store, _, _, clock, _ = stalled
    monkeypatch.setattr(local_slots, "live", lambda: {f"w{n}" for n in range(workers)})
    capped_watch(stalled)
    clock[0] += 600_000
    capped_watch(stalled)
    assert bool(spawn_stall.findings(store, "sw")) is raised
    assert bool(InboxStore(store.redis).pending_mail("master@sw")) is raised


def test_a_local_cap_refusal_before_the_last_observation_does_not_hold_the_lane(stalled, monkeypatch):
    store, _, _, clock, _ = stalled
    monkeypatch.setattr(local_slots, "live", lambda: {f"w{n}" for n in range(8)})
    spawn_stall.refuse_local(store, "sw", "ws", 8, 8, clock[0] - 1)
    capped_watch(stalled, refused=False)
    clock[0] += 60_000
    capped_watch(stalled, refused=False)
    clock[0] += 600_000
    capped_watch(stalled, refused=False)
    (finding,) = spawn_stall.findings(store, "sw")
    assert finding.kind == "spawn stall"


def test_a_local_cap_refusal_at_the_last_observation_holds_the_lane(stalled, monkeypatch):
    store, _, _, clock, _ = stalled
    monkeypatch.setattr(local_slots, "live", lambda: {f"w{n}" for n in range(8)})
    assert not spawn_stall.capped(store, "sw", 0)
    spawn_stall.refuse_local(store, "sw", "ws", 8, 8, clock[0])
    assert spawn_stall.capped(store, "sw", clock[0])
    assert not spawn_stall.capped(store, "sw", clock[0] + 1)


def test_a_local_cap_refusal_holds_the_first_observation(stalled, monkeypatch):
    store, ledger, runtime, clock, _ = stalled
    monkeypatch.setattr(local_slots, "live", lambda: {f"w{n}" for n in range(8)})
    spawn_stall.refuse_local(store, "sw", "ws", 8, 8, 0)
    assert "since" not in spawn_stall.observe(store, "sw", ledger, clock[0], runtime)


def test_a_local_cap_refusal_reads_the_refusing_machine(stalled, monkeypatch):
    store, _, _, clock, _ = stalled
    monkeypatch.setattr(local_slots, "live", lambda: {f"w{n}" for n in range(7)})
    store.redis.hset(local_slots.key("ws"), "other/t9", lease.now_ms(store))
    spawn_stall.refuse_local(store, "sw", "ws", 8, 8, clock[0])
    assert spawn_stall.capped(store, "sw", clock[0])
    spawn_stall.refuse_local(store, "sw", "elsewhere", 8, 8, clock[0])
    assert not spawn_stall.capped(store, "sw", clock[0])


def test_admission_refusals_without_a_failed_launch_are_named_in_the_alarm(stalled):
    store, ledger, _, clock, _ = stalled
    ledger.rows["t3"] = {**ledger.rows["t1"], "id": "t3"}
    quiet_watch(stalled)
    seed_refusals(store, clock)
    clock[0] += 600_000
    quiet_watch(stalled)
    (finding,) = spawn_stall.findings(store, "sw")
    assert finding.evidence == ("A lane has a free seat, quota and claimable work", REFUSALS)
    (mail,) = InboxStore(store.redis).pending_mail("master@sw")
    assert mail.text == (
        "Spawn stall on sw: No agent has launched for ten minutes. "
        f"A lane has a free seat, quota and claimable work; {REFUSALS}"
    )


def test_a_fresh_failed_launch_follows_the_admission_refusals(stalled):
    store, ledger, _, clock, _ = stalled
    ledger.rows["t3"] = {**ledger.rows["t1"], "id": "t3"}
    tick_failure(stalled)
    seed_refusals(store, clock)
    clock[0] += 600_000
    tick_failure(stalled)
    (finding,) = alarms(stalled)
    assert finding["evidence"] == [
        "A lane has a free seat, quota and claimable work",
        REFUSALS,
        "Last failed step tests.swarm.test_spawn_stall.stalled.<locals>.broken: RuntimeError: forced phase failure",
    ]


def test_refusals_last_seen_before_the_stall_are_not_named(stalled):
    store, _, _, clock, _ = stalled
    placement_waits.observe(store, "sw", "eng", "t1", CPU_WAIT, clock[0] - 5)
    spawn_stall.refuse_local(store, "sw", "ws", 8, 8, clock[0] - 1)
    placement_waits.observe(store, "sw", "ci", "t2", FULL_WAIT, clock[0] - 2)
    quiet_watch(stalled)
    placement_waits.observe(store, "sw", "ci", "t2", FULL_WAIT, clock[0])
    clock[0] += 600_000
    quiet_watch(stalled)
    (finding,) = spawn_stall.findings(store, "sw")
    assert finding.evidence[-1] == (
        "Admission refusals, latest first: cpu on anton-1 (1 task), max agents on anton-2 (1 task)"
    )
    assert placement_waits.read(store, "sw")["t2"]["started_at"] == clock[0] - 600_002


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("no hive for eng has room: anton-1 ram: 1 of 8 MB available", [("ram", "anton-1")]),
        ("no Claude account has room after hive sessions: 2", [("quota", "")]),
        ("no live hive takes eng, and the local machine runs 8 of its 8 agents", [("placement", "")]),
        (", and the local machine runs 8 of its 8 agents", []),
        ("no hive for eng has room", [("placement", "")]),
        (f"{CPU_WAIT}, and the local machine runs 8 of its 8 agents: ram: pod full", [("cpu", "anton-1")]),
        ("no hive has room, and the local machine runs 8 of its 8 agents", [("placement", "")]),
        ("no hive for eng has room: anton-1 cpu: load has room: none", [("cpu", "anton-1")]),
    ],
)
def test_each_refusal_names_its_reason_and_host(reason, expected):
    assert spawn_stall._refused(reason) == expected


def test_no_refusals_leave_no_evidence_line():
    assert spawn_stall.refusals({}, {}, 0) == ""


def test_a_legacy_wait_without_a_seen_time_counts_from_its_start():
    waits = {"t1": {"lane": "eng", "reason": CPU_WAIT, "started_at": 5}}
    assert spawn_stall.refusals(waits, {}, 5) == "Admission refusals, latest first: cpu on anton-1 (1 task)"
    assert spawn_stall.refusals(waits, {}, 6) == ""


def test_a_refusal_orders_by_its_latest_sighting_across_tasks():
    waits = {
        "t1": {"reason": CPU_WAIT, "started_at": 1, "seen_at": 9},
        "t2": {"reason": placement._refused(HIVE_FULL, [MAX_GATE]), "started_at": 1, "seen_at": 5},
        "t3": {"reason": CPU_WAIT, "started_at": 1, "seen_at": 1},
    }
    assert spawn_stall.refusals(waits, {}, 0) == (
        "Admission refusals, latest first: cpu on anton-1 (2 tasks), max agents on anton-2 (1 task)"
    )


def test_a_refusal_first_seen_at_zero_keeps_its_place():
    waits = {"t1": {"reason": placement._refused(HIVE_FULL, [MAX_GATE]), "started_at": 0, "seen_at": 0}}
    local = {"machine": "ws", "taken": 8, "maximum": 8, "at": 0}
    assert spawn_stall.refusals(waits, local, 0) == (
        "Admission refusals, latest first: local cap on ws (8 of 8 agents), max agents on anton-2 (1 task)"
    )


def test_a_local_cap_seen_at_the_stall_start_is_named():
    local = {"machine": "ws", "taken": 8, "maximum": 8, "at": 7}
    assert spawn_stall.refusals({}, local, 7) == "Admission refusals, latest first: local cap on ws (8 of 8 agents)"
    assert spawn_stall.refusals({}, local, 8) == ""
