from typing import Any

import pytest

from scripts.gates.progress import Progress
from scripts.swarm import metrics_ledger, status
from scripts.swarm.health import findings as health
from scripts.swarm.store import RedisStore

pytestmark = pytest.mark.feature("swarm-review-lane-reporting")
REVIEWER = "reviewer@abcdef-0001"


@pytest.mark.feature("swarm-review-lane-reporting")
def test_status_counts_reviewer_talk_after_an_outcome() -> None:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    marks = Progress(store.redis, "sw")
    marks.outcome(REVIEWER, "reviewed", 5)
    marks.talk(REVIEWER)
    assert status.talk_since_outcome(store, "sw", [{"name": REVIEWER, "lane": "review"}]) == {REVIEWER: 1}


@pytest.mark.parametrize("populated", [False, True])
@pytest.mark.feature("swarm-review-lane-reporting")
def test_ledger_metrics_include_zero_and_populated_review_counts(populated: bool) -> None:
    path = {"plan": "", "phase": "", "slice": "", "task": "tasks/r", "lane": "review"}
    nodes = [{"node": "tasks/r", "kind": "task", "state": "open"}] if populated else []
    rows = metrics_ledger.count_rows("sw", 1000, nodes, {"tasks/r": path})
    review = [row for row in rows if row["lane"] == "review" and row["state"] == "open"]
    assert len(review) == 1
    assert review[0]["value"] == float(populated)
    assert next(row["value"] for row in rows if row["lane"] == "" and row["state"] == "open") == float(populated)


@pytest.mark.feature("swarm-review-lane-reporting")
def test_health_uses_reviewer_worker_budget_and_names_its_findings() -> None:
    events: list[dict[str, Any]] = [{"kind": "comment edited", "target": "phases/p", "by": REVIEWER} for _ in range(25)]
    found = health.ceremony(events, {}, health.limits({}))
    assert len(found) == 1
    assert found[0].subject == REVIEWER
    assert found[0].kind == "ceremony"
    assert health.ceremony(events, {}, health.limits({}), talk={REVIEWER: 0}) == []
    assert not health._is_worker("master@abcdef-0002")
