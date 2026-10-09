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
    assert any("broken" in line and "forced phase failure" in line for line in finding["evidence"])
    mail = InboxStore(store.redis).pending_mail("master@sw")
    assert len(mail) == 1
    assert "forced phase failure" in mail[0].text
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
    assert ledger_events.findings_pass(inbox, store, "sw", alarms(stalled)) == []
    assert len(inbox.pending_mail("master@sw")) == 1


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
        with spawn_stall.watch(store, "sw", ledger, lambda: clock[0]):
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
    with timing.tick("sw"), spawn_stall.watch(store, "sw", ledger, lambda: clock[0]):
        tick._record_spawn_failure("sw", store, record, RuntimeError("forced launch failure"))
    clock[0] += 600_000
    with timing.tick("sw"), spawn_stall.watch(store, "sw", ledger, lambda: clock[0]):
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
