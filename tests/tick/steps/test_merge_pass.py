from copy import copy
from threading import Event
from typing import Any

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import cli, idle, ledger_events, linked_issues, waits
from scripts.swarm.store import AgentRecord, RedisStore
from scripts.tick import engine, pool, registry
from scripts.tick.deadline import Deadline
from scripts.tick.sites import _GraphView
from scripts.tick.step import Context, Result
from scripts.tick.steps import agents, host, report
from tests.swarm.test_cli import env
from tests.swarm.test_tick import FakeRuntime
from tests.tick.test_master_steps_kept import Step

_fixtures = env
pytestmark = pytest.mark.unit
AGENT = "engineer@a1b2c3-0001"
URL = "https://github.com/o/r/pull/7"
NOW = 5_000


@pytest.mark.feature("tick-merged-task-settlement-after-overrun")
@pytest.mark.parametrize("kind", ["code", "ci"])
def test_merge_pass(env: tuple[RedisStore, Any, FakeRuntime], monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    store, ledger, runtime = env
    cli.main(["sw", "create", "--repo", "/repo"])
    ledger.rows["t1"].update(state="pr", kind=kind, pr_url=URL, claimed_by=AGENT)
    store.claim("sw", "t1", AGENT, 60_000)
    store.put_agent("sw", AgentRecord(AGENT, "eng", "t1", state="working", seat="eng-1@sw"))
    runtime.live.add(AGENT)
    idle.declare_wait(store.redis, "sw", AGENT, NOW + 60_000, "merge", NOW, waits.on("merge", URL))
    pull = ledger_events.PullRequest("MERGED", 2, 1, False, True)
    monkeypatch.setattr(ledger_events, "tick_view", lambda *args: {URL: pull}.get)
    monkeypatch.setattr(linked_issues, "settle", lambda *args: [])
    monkeypatch.setattr(cli, "now_ms", lambda: NOW)
    box: dict[str, Any] = {}
    state = {
        "store": store,
        "slug": "sw",
        "ledger": ledger,
        "inbox": InboxStore(store.redis),
        "doc": {"tasks": list(ledger.rows.values())},
        "rows": ledger.rows,
        "view": None,
    }
    selected = [
        copy(step) for step in (*host.STEPS, *report.STEPS) if step.name in {"tick view", "waits end", "merge pass"}
    ]
    for step in selected:
        step.interval_s = 0.0
    release = Event()

    def blocked(ctx: Context) -> Result:
        release.wait(1.0)
        return Result()

    prefix = (Step("intent check", "wrapper intent", blocked, budget_s=0.03), Step("page relay", "wrapper relay"))
    sites = ("wrapper intent", "wrapper relay", "wrapper view", "wrapper events")
    steps = registry.site_edges((*prefix, *selected), sites, {"wrapper intent": "wrapper relay"})
    try:
        with pool.running(critical_s=0.3, slow_s=0.3):
            records = engine.run_graph(steps, Context(_GraphView(state, box), Deadline.after(5.0)))
    finally:
        release.set()
    observed = {record.name: record for record in records}
    assert observed["intent check"].abandoned
    assert observed["page relay"].skipped
    assert ledger.rows["t1"]["state"] == "done"
    assert idle.wait(store.redis, "sw", AGENT) is None
    assert {step.name for step in selected} <= observed.keys()
    assert all(observed[step.name].outcome == engine.OK for step in selected)

    def reap(ctx: Context) -> Result:
        agents._reap("sw", store, ledger, runtime, ledger.rows, NOW + 60_000)
        return Result()

    with pool.running(critical_s=1.0, slow_s=1.0):
        engine.run_graph((Step("reap", "agents", reap),), Context(_GraphView(state, box), Deadline.after(5.0)))
    assert store.agents("sw") == []
    assert AGENT not in runtime.live
    assert runtime.killed == [AGENT]
