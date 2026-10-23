import json

import pytest

from scripts.doctor import ci_speed, ci_speed_read, loop
from scripts.swarm.health.findings import Finding
from tests.doctor.test_ci_speed_incident import AFTER, details, items, status, waiting
from tests.doctor.test_ci_speed_timer import DOCTOR, WATCHED, snapshot, store  # noqa: F401

pytestmark = pytest.mark.unit
COOLDOWN_MS = 3600000
SCOPE = '["anton-oss-queue"]'


def judged(store, worst=1511):  # noqa: F811
    original = Finding("ci runner wait", SCOPE, "slow", ("text",), "runner wait", worst, evidence_identity=("1", worst))
    loop.record(store, DOCTOR, [original], AFTER, COOLDOWN_MS)
    judgments = loop.verdicts(store, DOCTOR)
    judgments.judge(original.id, "established", "runner cap is the operator's call", "reader", AFTER)
    return original, store.redis.hgetall(judgments.key)


def later(original, *runs_and_worst):
    *runs, worst = runs_and_worst
    return Finding(
        original.kind, original.subject, "slow", ("text",), "runner wait", worst, evidence_identity=(*runs, worst)
    )


def test_a_new_run_inside_the_judged_worst_raises_nothing(store):  # noqa: F811
    original, records = judged(store)
    assert ci_speed.unjudged([later(original, "2", 600)], records, AFTER + 300000, COOLDOWN_MS) == []


def test_a_new_run_beyond_the_judged_worst_and_margin_raises_again(store):  # noqa: F811
    original, records = judged(store)
    grown = later(original, "2", 2000)
    assert ci_speed.unjudged([grown], records, AFTER + 300000, COOLDOWN_MS) == [grown]


def test_the_margin_bounds_growth(store):  # noqa: F811
    original, records = judged(store, worst=1000)
    edge = round(1000 * (1 + ci_speed.GROWTH_MARGIN))
    assert ci_speed.unjudged([later(original, "2", edge)], records, AFTER + 1, COOLDOWN_MS) == []
    beyond = later(original, "2", edge + 1)
    assert ci_speed.unjudged([beyond], records, AFTER + 1, COOLDOWN_MS) == [beyond]


def test_the_cooldown_ending_returns_a_finding_inside_the_judged_range(store):  # noqa: F811
    original, records = judged(store)
    current = later(original, "2", 600)
    assert ci_speed.unjudged([current], records, AFTER + COOLDOWN_MS, COOLDOWN_MS) == [current]


def test_new_runs_in_range_send_the_masters_no_new_notice(store, monkeypatch):  # noqa: F811
    status(monkeypatch)

    def finished(identity, created):
        run = waiting(identity, created, labels=("anton-oss-queue",))
        run["jobs"][0].update(
            started_at="2026-10-19T17:00:00Z",
            completed_at="2026-10-19T17:01:00Z",
            status="completed",
            runner_name="runner",
        )
        run.update(status="completed", conclusion="success")
        return run

    records = [finished(1, "2026-10-19T16:34:49Z")]
    monkeypatch.setattr(
        ci_speed_read, "read", lambda *args, **kwargs: snapshot(records, complete=False, error="pagination pending")
    )
    store.redis.set(store.key(DOCTOR, "ci-speed"), json.dumps({"last": AFTER - 60000}))
    loop.speed(store, DOCTOR, WATCHED, AFTER)
    [finding] = [entry for entry in details(store) if entry["kind"] == "ci runner wait"]
    assert finding["measure"] == 1511
    loop.verdicts(store, DOCTOR).judge(finding["id"], "established", "operator cap", "reader", AFTER)
    records.append(finished(2, "2026-10-19T16:50:00Z"))
    for minutes in (1, 6, 11):
        loop.speed(store, DOCTOR, WATCHED, AFTER + minutes * 60000)
    for texts in items(store).values():
        assert len(texts) == 1
    records.append(finished(3, "2026-10-19T16:26:40Z"))
    loop.speed(store, DOCTOR, WATCHED, AFTER + 16 * 60000)
    for texts in items(store).values():
        assert len(texts) == 2
        assert "ci-runner-wait/" in texts[1] and "worst 2000 seconds" in texts[1]
