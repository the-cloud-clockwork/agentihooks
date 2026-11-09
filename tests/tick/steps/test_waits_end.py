from copy import copy
from types import SimpleNamespace

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import cli, idle, waits
from scripts.swarm.store import AgentRecord
from scripts.tick import engine, pool
from scripts.tick.deadline import Deadline
from scripts.tick.sites import _GraphView
from scripts.tick.step import Context
from scripts.tick.steps import report
from tests.swarm.test_ready import AGENT, MERGED, NOW, URL, merging

_fixtures = merging
pytestmark = pytest.mark.unit


@pytest.mark.feature("tick-merged-task-settlement-after-overrun")
def test_waits_end(merging: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    store = merging.store
    store.put_agent("sw", AgentRecord(AGENT, "eng", "t1", state="working"))
    idle.declare_wait(store.redis, "sw", AGENT, NOW + 60_000, "merge", NOW, waits.on("merge", URL))
    monkeypatch.setattr(cli, "now_ms", lambda: NOW)
    state = {
        "store": store,
        "slug": "sw",
        "rows": merging.rows,
        "inbox": InboxStore(store.redis),
        "view": {URL: MERGED}.get,
    }
    step = copy(next(step for step in report.STEPS if step.name == "waits end"))
    step.interval_s = 0.0
    with pool.running(critical_s=0.0, slow_s=1.0):
        records = engine.run_graph((step,), Context(_GraphView(state, {}), Deadline.after(5.0)))
    assert records[0].outcome == engine.OK
    assert idle.wait(store.redis, "sw", AGENT) is None
    assert merging.rows["t1"]["state"] == "pr"
