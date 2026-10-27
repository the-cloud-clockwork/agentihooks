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
    return record([work], event="merge_group", identity=identity, status="completed", conclusion=conclusion)


def mentions(database, slug, kind):
    counts = Counter()
    for item in InboxStore(database.redis).inbox(f"master@{slug}"):
        for line in item.text.splitlines():
            if line.startswith(f"- {kind.replace(' ', '-')}/"):
                counts.update(set(re.findall(r"\brun (\d+)", line)))
    return counts


@pytest.mark.parametrize("kind", ["ci runner wait", "ci tier wall time"])
@pytest.mark.parametrize("judged", [False, True])
def test_finished_runs_are_mentioned_once_when_group_members_join_leave_or_return(store, monkeypatch, kind, judged):  # noqa: F811
    monkeypatch.setattr(loop.github_status, "unresolved", lambda: [])
    batch = []
    monkeypatch.setattr(ci_speed_read, "read", lambda *args, **kwargs: snapshot(batch))
    first, second = finished(101, kind), finished(102, kind)
    batch[:] = [first]
    loop.speed(store, DOCTOR, WATCHED, NOW)
    if judged:
        details = store.redis.hgetall(store.key(DOCTOR, "doctor-findings"))
        for identity in details:
            if identity.startswith(f"{kind.replace(' ', '-')}/"):
                loop.verdicts(store, DOCTOR).judge(identity, "established", "investigated", "reader", NOW)
    batch[:] = [second, finished(103, kind, "cancelled"), finished(104, kind, "superseded")]
    loop.speed(RedisStore(store.redis), DOCTOR, WATCHED, NOW + 60000)
    expected = {"101": 1} if judged else {"101": 1, "102": 1}
    for slug in (DOCTOR, WATCHED):
        assert mentions(store, slug, kind) == expected
    state = json.loads(store.redis.get(store.key(DOCTOR, "ci-speed")))
    group = next(entry for entry in state["groups"].values() if entry["kind"] == kind)
    assert {member["run"] for member in group["members"].values()} == {"101", "102"}
    batch[:] = [second]
    loop.speed(RedisStore(store.redis), DOCTOR, WATCHED, NOW + 3600000)
    batch[:] = [first, second]
    loop.speed(RedisStore(store.redis), DOCTOR, WATCHED, NOW + 3660000)
    for slug in (DOCTOR, WATCHED):
        assert mentions(store, slug, kind) == expected


def test_an_unconfirmed_group_does_not_hide_finished_groups_later_in_the_pass(store, monkeypatch):  # noqa: F811
    monkeypatch.setattr(loop.github_status, "unresolved", lambda: [])
    batch = [record([job(0, queued=True, created=-1200)], identity=99), finished(101, "ci runner wait")]
    monkeypatch.setattr(ci_speed_read, "read", lambda *args, **kwargs: snapshot(batch))
    loop.speed(store, DOCTOR, WATCHED, NOW)
    for slug in (DOCTOR, WATCHED):
        for kind in ("ci runner wait", "ci tier wall time"):
            assert mentions(store, slug, kind) == {"101": 1}


def test_independent_doctors_keep_their_own_finished_run_notice_history(store, monkeypatch):  # noqa: F811
    monkeypatch.setattr(loop.github_status, "unresolved", lambda: [])
    batch = [finished(101, "ci runner wait")]
    monkeypatch.setattr(ci_speed_read, "read", lambda *args, **kwargs: snapshot(batch))
    loop.speed(store, DOCTOR, WATCHED, NOW)
    loop.speed(store, "other-doctor", WATCHED, NOW)
    for kind in ("ci runner wait", "ci tier wall time"):
        assert mentions(store, WATCHED, kind) == {"101": 2}
        assert mentions(store, DOCTOR, kind) == {"101": 1}
        assert mentions(store, "other-doctor", kind) == {"101": 1}
