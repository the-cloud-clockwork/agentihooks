import json
import re
from collections import Counter

import pytest

from scripts.doctor import ci_speed_read, loop
from scripts.inbox.store import InboxStore
from scripts.swarm.store import RedisStore
from tests.doctor.test_ci_speed import NOW, job, record, stamp
from tests.doctor.test_ci_speed_timer import DOCTOR, WATCHED, snapshot, store  # noqa: F401

pytestmark = pytest.mark.unit


def finished(identity, kind, conclusion="success"):
    work = job(400 if kind == "ci runner wait" else 0, created=-1200)
    work.update(status="completed", completed_at=stamp(-1))
    return record([work], identity=identity, status="completed", conclusion=conclusion)


def mentions(database, slug, kind):
    counts = Counter()
    for item in InboxStore(database.redis).inbox(f"master@{slug}"):
        for line in item.text.splitlines():
            if line.startswith(f"- {kind.replace(' ', '-')}/"):
                counts.update(set(re.findall(r"\brun (\d+)", line)))
    return counts


@pytest.mark.parametrize("kind", ["ci runner wait", "ci tier wall time"])
def test_finished_runs_are_mentioned_once_when_group_members_join_leave_or_return(store, monkeypatch, kind):  # noqa: F811
    monkeypatch.setattr(loop.github_status, "unresolved", lambda: [])
    batch = []
    monkeypatch.setattr(ci_speed_read, "read", lambda *args, **kwargs: snapshot(batch))
    first, second = finished(101, kind), finished(102, kind)
    batch[:] = [first]
    loop.speed(store, DOCTOR, WATCHED, NOW)
    batch[:] = [second, finished(103, kind, "cancelled"), finished(104, kind, "superseded")]
    loop.speed(RedisStore(store.redis), DOCTOR, WATCHED, NOW + 60000)
    for slug in (DOCTOR, WATCHED):
        assert mentions(store, slug, kind) == {"101": 1, "102": 1}
    state = json.loads(store.redis.get(store.key(DOCTOR, "ci-speed")))
    group = next(entry for entry in state["groups"].values() if entry["kind"] == kind)
    assert {member["run"] for member in group["members"].values()} == {"101", "102"}
    batch[:] = [first, second]
    loop.speed(RedisStore(store.redis), DOCTOR, WATCHED, NOW + 3600000)
    for slug in (DOCTOR, WATCHED):
        assert mentions(store, slug, kind) == {"101": 1, "102": 1}
