from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import ledger_events, master_retire, outage, pane, provider_probe, status
from scripts.swarm.health.findings import Finding
from scripts.swarm.store import AgentRecord
from scripts.swarm.tick import IDLE_NUDGE_TICKS, tick
from tests.swarm.test_lifetime import HOUR, idle_master
from tests.swarm.test_tick import FakeLedger, FakeRuntime, tasks, workers
from tests.swarm.test_tick import store as store

NAME = "engineer@a1b2c3-0001"
MINUTE = 60_000


class ProviderDown(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.errors = {}

    def observe(self, agent):
        return replace(super().observe(agent), provider_error=self.errors.get(agent.name, ""))


@pytest.fixture(autouse=True)
def outages(tmp_path, monkeypatch):
    monkeypatch.setattr(outage, "HOME", tmp_path)


def claimed(store):
    ledger, runtime = tasks(("t1", "eng")), ProviderDown()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    store.update("sw", state="paused")
    runtime.statuses[NAME] = "idle"
    return ledger, runtime


def idle_ticks(store):
    return [a.idle_ticks for a in workers(store)]


def test_an_agent_at_a_provider_error_for_twenty_ticks_keeps_its_claim_unnudged_and_unretired(store):
    ledger, runtime = claimed(store)
    runtime.errors[NAME] = "API Error: 529"
    for n in range(20):
        tick("sw", store, ledger, runtime, now_ms=2_000 + n * MINUTE)
    assert runtime.nudged == [] and runtime.killed == []
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("claimed", NAME)
    assert idle_ticks(store) == [0]
    assert store.redis.get(store.key("sw", outage.PANE, NAME)) == "API Error: 529"


def test_an_open_outage_holds_the_idle_count_and_counting_resumes_where_it_stopped(store):
    ledger, runtime = claimed(store)
    for n in range(IDLE_NUDGE_TICKS - 1):
        tick("sw", store, ledger, runtime, now_ms=2_000 + n * MINUTE)
    outage.observe("sw", "redis", ["Redis ping check failed: ConnectionError"], 10 * MINUTE)
    for n in range(20):
        tick("sw", store, ledger, runtime, now_ms=10 * MINUTE + n * MINUTE)
    assert idle_ticks(store) == [IDLE_NUDGE_TICKS - 1] and runtime.nudged == []
    outage.observe("sw", "redis", [], 40 * MINUTE)
    tick("sw", store, ledger, runtime, now_ms=40 * MINUTE)
    assert idle_ticks(store) == [IDLE_NUDGE_TICKS] and runtime.nudged == [NAME]


def test_a_cleared_provider_error_lets_the_idle_count_run_again(store):
    ledger, runtime = claimed(store)
    runtime.errors[NAME] = "Connection error."
    tick("sw", store, ledger, runtime, now_ms=2_000)
    runtime.errors[NAME] = ""
    tick("sw", store, ledger, runtime, now_ms=3_000)
    assert idle_ticks(store) == [1]
    assert store.redis.get(store.key("sw", outage.PANE, NAME)) == ""


def test_an_idle_master_is_not_retired_while_an_outage_is_open(store, monkeypatch):
    monkeypatch.setenv(master_retire.MINUTES, "0")
    agent, runtime = idle_master(store, "paused")
    outage.observe("sw", "provider", ["master: API Error: 503"], 1)
    tick("sw", store, FakeLedger([]), runtime, 6 * HOUR + 2)
    assert [a.name for a in store.agents("sw")] == [agent.name] and runtime.killed == []
    outage.observe("sw", "provider", [], 6 * HOUR + 3)
    tick("sw", store, FakeLedger([]), runtime, 6 * HOUR + 4)
    assert store.agents("sw") == [] and runtime.killed == [agent.name]


def test_held_names_open_outages_before_the_agents_own_pane(store):
    assert outage.held(store, "sw", NAME) == ""
    store.redis.set(store.key("sw", outage.PANE, NAME), "API Error: 529")
    assert outage.held(store, "sw", NAME) == "API Error: 529"
    outage.observe("sw", "redis", ["Redis ping check failed: TimeoutError"], 1)
    outage.observe("sw", "provider", ["worker (codex): stream disconnected before completion"], 2)
    assert outage.held(store, "sw", NAME) == (
        "Redis ping check failed: TimeoutError; worker (codex): stream disconnected before completion"
    )


def test_paused_time_counts_each_outage_moment_once_inside_the_window(store):
    outage.observe("sw", "redis", ["down"], 100)
    outage.observe("sw", "redis", [], 400)
    outage.observe("sw", "provider", ["down"], 300)
    assert outage.paused_ms("sw", 0, 1_000) == 900
    assert outage.paused_ms("sw", 200, 1_000) == 800
    outage.observe("sw", "provider", [], 600)
    assert outage.paused_ms("sw", 0, 1_000) == 500
    assert outage.paused_ms("sw", 500, 550) == 50
    assert outage.paused_ms("sw", 700, 1_000) == 0
    assert outage.paused_ms("other", 0, 1_000) == 0


def test_paused_time_keeps_each_closed_outage_of_one_kind(store):
    outage.observe("sw", "redis", ["down"], 100)
    outage.observe("sw", "redis", [], 200)
    outage.observe("sw", "redis", ["down again"], 500)
    outage.observe("sw", "redis", [], 700)
    assert outage.paused_ms("sw", 0, 1_000) == 300
    outage.observe("sw", "redis", ["down a third time"], 900)
    outage.observe("sw", "redis", ["still down"], 950)
    assert outage.paused_ms("sw", 0, 1_000) == 400
    assert outage.read("sw")["redis"]["started_at"] == 900


def test_paused_time_keeps_the_newest_closed_outages_of_one_kind(store):
    for n in range(outage.PAST_SPANS + 10):
        outage.observe("sw", "redis", ["down"], n * 10)
        outage.observe("sw", "redis", [], n * 10 + 5)
    outage.observe("sw", "redis", ["down"], 10_000)
    past = outage.read("sw")["redis"]["past"]
    assert len(past) == outage.PAST_SPANS and past[-1] == [
        (outage.PAST_SPANS + 9) * 10,
        (outage.PAST_SPANS + 9) * 10 + 5,
    ]


def test_a_pane_error_mark_expires_within_a_day(store):
    outage.mark_pane(store, "sw", NAME, "API Error: 529", 1_000)
    assert 0 < store.redis.ttl(store.key("sw", outage.PANE, NAME)) <= outage.PANE_TTL_S
    assert 0 < store.redis.ttl(store.key("sw", outage.PANE_SINCE, NAME)) <= outage.PANE_TTL_S


def test_a_new_provider_episode_keeps_the_closed_one_in_paused_time(store, monkeypatch):
    monkeypatch.setattr(provider_probe, "check", lambda target, directory: provider_probe.Result(False, "down", 0))
    with outage._edit("sw") as records:
        records["provider"] = {"kind": "provider", "started_at": 100, "evidence": ["one"], "ended_at": 300}
    agent = SimpleNamespace(name="one", state="active", harness="claude", account="alpha", model="chosen")
    error = pane.PaneObservation(
        "idle", provider_error="API Error: 529", provider_checked=True, provider_error_id="two"
    )
    outage.providers(SimpleNamespace(agents=lambda slug: [agent]), "sw", SimpleNamespace(observe=lambda a: error), 600)
    assert outage.read("sw")["provider"]["started_at"] == 600
    assert outage.paused_ms("sw", 0, 1_000) == 600


def test_idle_findings_for_an_agent_held_by_an_outage_carry_it(store):
    store.put_agent("sw", AgentRecord(NAME, "eng", "t1", idle_ticks=3, started_at=1))
    rows = [{"id": "t1", "title": "Hold", "state": "claimed", "claimed_by": NAME}]
    found = [f for f in status.findings(store, "sw", store.config("sw"), rows, []) if f["subject"] == NAME]
    assert [f["kind"] for f in found] == ["idle with claim"]
    store.redis.set(store.key("sw", outage.PANE, NAME), "API Error: 529")
    found = [f for f in status.findings(store, "sw", store.config("sw"), rows, []) if f["subject"] == NAME]
    assert [(f["kind"], f["summary"], f["evidence"]) for f in found] == [
        (
            "idle during outage",
            "idle for 3 ticks while holding a task, held by an outage",
            ["task Hold (claimed)", "outage: API Error: 529"],
        )
    ]


def test_a_stale_claim_finding_is_held_through_the_claimant_of_its_task(store):
    agents = [AgentRecord(NAME, "eng", "t1"), AgentRecord("engineer@a1b2c3-0002", "eng", "t2")]
    stale = Finding("stale claim", "t1", "no progress for 40 minutes", ("task t1",), "30 minutes", 40)
    other = replace(stale, subject="t2")
    unrelated = Finding("ceremony", NAME, "too many moves", (), "20", 21)
    store.redis.set(store.key("sw", outage.PANE, NAME), "API Error: 529")
    assert outage.hold_findings(store, "sw", [stale, other, unrelated], agents) == [
        Finding(
            "stale during outage",
            "t1",
            "no progress for 40 minutes, held by an outage",
            ("task t1", "outage: API Error: 529"),
            "30 minutes",
            40,
        ),
        other,
        unrelated,
    ]


def test_the_findings_pass_sends_nothing_for_findings_held_by_an_outage(store):
    store.put_agent("sw", AgentRecord(NAME, "eng", "t1"))
    inbox = InboxStore(store.redis)
    shown = [
        {"id": f"{kind.replace(' ', '-')}/{NAME}", "kind": kind, "subject": NAME, "summary": "idle"}
        for kind in ("idle during outage", "stale during outage")
    ]
    assert ledger_events.findings_pass(inbox, store, "sw", shown) == []
    assert inbox.pending() == []
