import pytest

from scripts.inbox import store as inbox_store
from scripts.inbox.store import InboxStore
from scripts.swarm import capacity, cli, status
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
    assert wake.decide(item, None, inbox.history(item.id), clock[0] + 300_000, 300_000) is None


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
        "summary": "A worker is idle",
        "verdict": None,
    }
    sent = ledger_events.findings_pass(inbox, store, "sw", alarms(stalled) + [other])
    assert sent == ["told master@sw: finding:idle/worker:0"]
    assert len(inbox.pending_mail("master@sw")) == 2


@pytest.mark.parametrize("unavailable", ["paused", "stopped", "seat", "quota", "harness", "task"])
def test_unavailable_launch_conditions_reset_the_alarm(stalled, monkeypatch, unavailable):
    store, ledger, _, clock, _ = stalled
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
        tick._record_spawn_failure("sw", store, record, RuntimeError("forced launch failure"))
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
    assert spawn_stall.eligible(store, "sw", ledger, clock[0], runtime)
    assert received == [previous]


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
