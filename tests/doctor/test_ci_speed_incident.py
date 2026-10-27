import io
import json
import time
import urllib.request
from datetime import datetime

import pytest

from scripts.doctor import ci_speed, ci_speed_read, cli, loop
from scripts.inbox.store import InboxStore
from scripts.swarm.ledger_events import iso_ms
from tests.doctor.test_ci_speed_timer import DOCTOR, WATCHED, snapshot, store  # noqa: F401

pytestmark = pytest.mark.unit
GITHUB_STATUS_READ = True

INCIDENT = {
    "id": "8tzbpcnl75jp",
    "name": "Degraded availability for GitHub Actions, GitHub Pages and Webhooks",
    "status": "investigating",
    "created_at": "2026-10-19T15:49:25.828Z",
    "started_at": "2026-10-19T15:49:25.822Z",
    "resolved_at": None,
    "shortlink": "https://stspg.io/nrv2k8s68bwn",
    "impact": "critical",
    "components": [{"name": "Webhooks"}, {"name": "Actions"}, {"name": "Pages"}],
}
PAGES = {**INCIDENT, "id": "pages", "name": "Disruption with GitHub Pages", "components": [{"name": "Pages"}]}
DURING = int(datetime.fromisoformat("2026-10-19T16:10:00+00:00").timestamp() * 1000)
AFTER = int(datetime.fromisoformat("2026-10-19T17:00:00+00:00").timestamp() * 1000)


def status(monkeypatch, *incidents, down=False):
    def urlopen(url, timeout=None):
        assert url == "https://www.githubstatus.com/api/v2/incidents/unresolved.json"
        if down:
            raise OSError("status page unreachable")
        return io.BytesIO(json.dumps({"incidents": list(incidents)}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)


def waiting(identity, created, labels=("self-hosted", "anton-oss")):
    queued = {
        "id": identity * 10,
        "name": "unit",
        "created_at": created,
        "started_at": None,
        "completed_at": None,
        "status": "queued",
        "labels": list(labels),
    }
    return {"id": identity, "event": "push", "path": ".github/workflows/test.yml", "status": "queued", "jobs": [queued]}


def runs(monkeypatch, records):
    monkeypatch.setattr(ci_speed_read, "read", lambda *args, **kwargs: snapshot(records))


def items(store):  # noqa: F811
    return {slug: [item.text for item in InboxStore(store.redis).inbox(f"master@{slug}")] for slug in (WATCHED, DOCTOR)}


def details(store):  # noqa: F811
    return [json.loads(raw) for raw in store.redis.hgetall(store.key(DOCTOR, "doctor-details")).values()]


def test_an_open_actions_incident_holds_speed_findings_and_says_so_once(store, monkeypatch):  # noqa: F811
    status(monkeypatch, INCIDENT)
    runs(monkeypatch, [waiting(n, "2026-10-19T15:51:00Z") for n in range(1, 21)])
    first = cli.timer(store, DOCTOR, DURING, False)
    cli.timer(store, DOCTOR, DURING + 60000, False)
    assert details(store) == []
    for texts in items(store).values():
        [text] = texts
        assert "held" in text and INCIDENT["name"] in text and "15:49 UTC" in text
        assert INCIDENT["shortlink"] in text
    assert any("20 runs read" in action and "held during GitHub incident" in action for action in first)


def test_after_the_incident_resolves_one_grouped_finding_labels_runs_from_its_window(store, monkeypatch):  # noqa: F811
    status(monkeypatch, INCIDENT)
    runs(monkeypatch, [])
    cli.timer(store, DOCTOR, DURING, False)
    status(monkeypatch)
    runs(monkeypatch, [waiting(n, "2026-10-19T16:40:00Z") for n in range(1, 6)])
    cli.timer(store, DOCTOR, AFTER, False)
    [finding] = details(store)
    assert finding["measure"] == 1200
    assert "5 runs" in finding["evidence"][0]
    assert "5 of these runs had jobs created during the GitHub incident" in finding["evidence"][1]
    for texts in items(store).values():
        assert sum(text.startswith("New Doctor findings") for text in texts) == 1


def test_ten_runs_on_one_label_in_one_pass_are_one_finding(store, monkeypatch):  # noqa: F811
    status(monkeypatch)
    created = ["2026-10-19T16:43:00Z", "2026-10-19T16:44:00Z", "2026-10-19T16:42:00Z"] + ["2026-10-19T16:44:30Z"] * 7
    runs(monkeypatch, [waiting(n, stamp) for n, stamp in enumerate(created, 1)])
    actions = cli.timer(store, DOCTOR, AFTER, False)
    [finding] = details(store)
    assert finding["kind"] == "ci queue age unconfirmed"
    assert finding["summary"] == "anton-oss, self-hosted jobs read as queued up to 18.00 minutes in 10 runs"
    assert finding["evidence"][0] == (
        "10 runs, anton-oss, self-hosted, worst 1080 seconds, median 930.0 seconds, runs 1, 2, 3, 4, 5, 6, 7, 8, 9, 10"
    )
    assert finding["measure"] == 1080
    assert len(finding["evidence"]) == 11
    assert any("1 crossings" in action for action in actions)
    for texts in items(store).values():
        [text] = texts
        assert text.count("\n- ") == 1


def test_a_wait_across_a_window_boundary_keeps_one_finding_and_one_notice(store, monkeypatch):  # noqa: F811
    status(monkeypatch)
    runs(monkeypatch, [waiting(1, "2026-10-19T16:40:00Z")])
    before = cli.timer(store, DOCTOR, AFTER - 60000, False)
    after = cli.timer(store, DOCTOR, AFTER, False)
    assert any("1 crossings" in action for action in before)
    assert any("1 crossings" in action for action in after)
    assert [finding["id"] for finding in details(store)] == ['ci-queue-age-unconfirmed/["anton-oss", "self-hosted"]']
    for texts in items(store).values():
        assert len(texts) == 1


def test_groups_keep_label_sets_and_tiers_apart_and_grow_without_another_notice(store, monkeypatch):  # noqa: F811
    status(monkeypatch)
    batch = waiting(3, "2026-10-19T16:40:00Z", labels=["anton-batch"])
    wall = {
        "id": 4,
        "event": "merge_group",
        "status": "completed",
        "conclusion": "success",
        "path": ".github/workflows/test.yml",
        "jobs": [{**waiting(4, "2026-10-19T16:40:00Z")["jobs"][0], "status": "in_progress", "runner_name": "r"}],
    }
    wall["jobs"][0]["started_at"] = "2026-10-19T16:41:00Z"
    wall["jobs"][0]["completed_at"] = "2026-10-19T17:00:00Z"
    runs(monkeypatch, [waiting(1, "2026-10-19T16:40:00Z"), batch, wall])
    cli.timer(store, DOCTOR, AFTER, False)
    assert sorted(finding["kind"] for finding in details(store)) == [
        "ci queue age unconfirmed",
        "ci queue age unconfirmed",
        "ci tier wall time",
    ]
    [tier] = [finding for finding in details(store) if finding["kind"] == "ci tier wall time"]
    assert tier["summary"] == "queue tier took up to 20.00 minutes in 1 run"
    runs(monkeypatch, [waiting(2, "2026-10-19T16:42:00Z")])
    cli.timer(store, DOCTOR, AFTER + 60000, False)
    [grown] = [f for f in details(store) if "anton-oss" in f["summary"]]
    assert "2 runs" in grown["evidence"][0] and "runs 1, 2" in grown["evidence"][0]
    for texts in items(store).values():
        assert len(texts) == 1


def test_judged_runs_do_not_notify_again_in_the_next_window(store, monkeypatch):  # noqa: F811
    status(monkeypatch)
    records = []
    for identity in (1, 2):
        run = waiting(identity, "2026-10-19T16:40:00Z")
        run.update(event="merge_group", status="completed", conclusion="success")
        run["jobs"][0].update(
            started_at="2026-10-19T16:59:00Z",
            completed_at="2026-10-19T17:00:00Z",
            status="completed",
            runner_name="runner",
        )
        records.append(run)
    monkeypatch.setattr(
        ci_speed_read,
        "read",
        lambda *args, **kwargs: snapshot(records, complete=False, error="pagination pending"),
    )
    store.redis.set(store.key(DOCTOR, "ci-speed"), json.dumps({"last": AFTER - 60000}))
    loop.speed(store, DOCTOR, WATCHED, AFTER)
    judgments = loop.verdicts(store, DOCTOR)
    for finding in details(store):
        if finding["kind"] in ("ci runner wait", "ci tier wall time"):
            judgments.judge(finding["id"], "established", "already investigated", "reader", AFTER)
    assert all(len(texts) == 1 for texts in items(store).values())
    loop.speed(store, DOCTOR, WATCHED, AFTER + 600000)
    for texts in items(store).values():
        assert len(texts) == 1


@pytest.mark.parametrize("kind,scope", [("ci tier wall time", "merge_group"), ("ci runner wait", '["fleet/queue"]')])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize(
    "change,expected",
    [
        ({}, False),
        ({"evidence_identity": ("1", 899), "measure": 899}, False),
        ({"evidence_identity": ("1", "2", "3", 900)}, False),
        ({"evidence_identity": ("1", "2 attempt 2", 900)}, False),
        ({"measure": 901}, False),
        ({"evidence_identity": ("1", "2", 1080)}, False),
        ({"evidence_identity": ("1", "2", 1081)}, True),
        ({"subject": "200-pull_request"}, True),
        ({"kind": "ci queue age unconfirmed"}, True),
        ({"elapsed": 60000}, True),
    ],
)
def test_grouped_speed_return_uses_the_judged_scope_and_measure(store, change, expected, legacy, kind, scope):  # noqa: F811
    from dataclasses import replace

    from scripts.swarm.health.findings import Finding

    original = Finding(
        kind,
        f"100-{scope}",
        "queue tier slow",
        ("2 runs, pool, runs legacy, worst 900 seconds, median 900 seconds, runs 1, 2",)
        if legacy
        else ("human readable wording changed",),
        "wall time",
        900,
        evidence_identity=("1", "2", 900),
    )
    loop.record(store, DOCTOR, [original], AFTER, 60000)
    judgments = loop.verdicts(store, DOCTOR)
    judgments.judge(original.id, "established", "investigated", "reader", AFTER)
    if legacy:
        prior = json.loads(store.redis.hget(judgments.key, original.id))
        del prior["evidence_identity"], prior["verdict"]["evidence_identity"]
        store.redis.hset(judgments.key, original.id, json.dumps(prior))
    elapsed = change.get("elapsed", 59999)
    current = replace(original, subject=f"200-{scope}")
    current = replace(current, **{key: value for key, value in change.items() if key != "elapsed"})
    found = ci_speed.unjudged([current], store.redis.hgetall(judgments.key), AFTER + elapsed, 60000)
    assert found == ([current] if expected else [])


@pytest.mark.parametrize("change", ["new run", "worse wait"])
def test_judged_group_stays_silent_for_new_finished_runs_inside_its_range(store, monkeypatch, change):  # noqa: F811
    status(monkeypatch)
    run = waiting(1, "2026-10-19T16:40:00Z")
    run["jobs"][0].update(
        started_at="2026-10-19T16:59:00Z",
        completed_at="2026-10-19T17:00:00Z",
        status="completed",
        runner_name="runner",
    )
    run.update(status="completed", conclusion="success")
    records = [run]
    monkeypatch.setattr(
        ci_speed_read,
        "read",
        lambda *args, **kwargs: snapshot(records, complete=False, error="pagination pending"),
    )
    store.redis.set(store.key(DOCTOR, "ci-speed"), json.dumps({"last": AFTER - 60000}))
    loop.speed(store, DOCTOR, WATCHED, AFTER)
    [finding] = [entry for entry in details(store) if entry["kind"] == "ci runner wait"]
    loop.verdicts(store, DOCTOR).judge(finding["id"], "established", "investigated", "reader", AFTER)
    if change == "new run":
        records.append({**run, "id": 2})
    else:
        run["jobs"][0]["created_at"] = "2026-10-19T16:39:59Z"
    loop.speed(store, DOCTOR, WATCHED, AFTER + 60000)
    loop.speed(store, DOCTOR, WATCHED, AFTER + 120000)
    records.append({**run, "id": 3})
    loop.speed(store, DOCTOR, WATCHED, AFTER + 180000)
    for texts in items(store).values():
        assert len(texts) == 1


def test_a_status_page_read_failure_never_holds_findings(store, monkeypatch):  # noqa: F811
    status(monkeypatch, down=True)
    runs(monkeypatch, [waiting(n, "2026-10-19T15:51:00Z") for n in range(1, 3)])
    actions = cli.timer(store, DOCTOR, DURING, False)
    [finding] = details(store)
    assert "2 runs" in finding["evidence"][0]
    assert not any("held" in action for action in actions)
    for texts in items(store).values():
        [text] = texts
        assert text.startswith("New Doctor findings")


def test_an_incident_outside_actions_does_not_hold_findings(store, monkeypatch):  # noqa: F811
    status(monkeypatch, PAGES)
    runs(monkeypatch, [waiting(1, "2026-10-19T15:51:00Z")])
    cli.timer(store, DOCTOR, DURING, False)
    assert len(details(store)) == 1
    for texts in items(store).values():
        [text] = texts
        assert text.startswith("New Doctor findings")


def test_an_incident_named_for_actions_without_components_holds_findings(store, monkeypatch):  # noqa: F811
    status(monkeypatch, {**INCIDENT, "name": "Actions Job Delays", "components": []})
    runs(monkeypatch, [waiting(1, "2026-10-19T15:51:00Z")])
    cli.timer(store, DOCTOR, DURING, False)
    assert details(store) == []


def test_a_resolved_incident_listed_again_holds_without_a_second_notice(store, monkeypatch):  # noqa: F811
    status(monkeypatch, INCIDENT)
    runs(monkeypatch, [])
    cli.timer(store, DOCTOR, DURING, False)
    status(monkeypatch)
    cli.timer(store, DOCTOR, DURING + 60000, False)
    status(monkeypatch, INCIDENT)
    runs(monkeypatch, [waiting(1, "2026-10-19T15:51:00Z")])
    actions = cli.timer(store, DOCTOR, DURING + 120000, False)
    assert details(store) == []
    assert any("held during GitHub incident" in action for action in actions)
    for texts in items(store).values():
        assert len(texts) == 1


@pytest.fixture
def eastern(monkeypatch):
    monkeypatch.setenv("TZ", "EST5")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def notice(incident, began):
    return (
        f"Doctor CI speed findings on the swarm {WATCHED} are held: GitHub reports an unresolved incident "
        f"affecting Actions, {incident['name']}, since {began}. {incident['shortlink']} "
        "Runs are still read and counted; findings resume when it resolves, and runs with jobs created "
        "during it are labelled."
    )


def test_each_new_incident_gets_its_own_notice_in_utc_and_the_pass_names_every_hold(store, monkeypatch, eastern):  # noqa: F811
    second = {
        **INCIDENT,
        "id": "second",
        "name": "Actions Job Delays",
        "started_at": "2026-10-19T16:05:00Z",
        "shortlink": "https://stspg.io/second",
    }
    status(monkeypatch, INCIDENT)
    runs(monkeypatch, [])
    loop.speed(store, DOCTOR, WATCHED, DURING)
    status(monkeypatch, INCIDENT, second)
    [action] = loop.speed(store, DOCTOR, WATCHED, DURING + 60000)
    assert action.endswith(
        f"; findings held during GitHub incident {INCIDENT['name']}; findings held during GitHub incident Actions Job Delays"
    )
    for texts in items(store).values():
        assert sorted(texts) == sorted(
            [notice(INCIDENT, "19 October 15:49 UTC"), notice(second, "19 October 16:05 UTC")]
        )


def test_an_unread_status_page_keeps_an_open_incident_window_without_holding(store, monkeypatch):  # noqa: F811
    status(monkeypatch, INCIDENT)
    runs(monkeypatch, [])
    loop.speed(store, DOCTOR, WATCHED, DURING)
    status(monkeypatch, down=True)
    runs(monkeypatch, [waiting(1, "2026-10-19T15:55:00Z")])
    loop.speed(store, DOCTOR, WATCHED, DURING + 60000)
    [finding] = details(store)
    assert finding["evidence"][1] == f"1 of these runs had jobs created during the GitHub incident {INCIDENT['name']}"


def test_runs_created_after_an_incident_resolves_are_not_labelled(store, monkeypatch):  # noqa: F811
    status(monkeypatch, INCIDENT)
    runs(monkeypatch, [])
    loop.speed(store, DOCTOR, WATCHED, DURING)
    status(monkeypatch)
    loop.speed(store, DOCTOR, WATCHED, DURING + 60000)
    runs(monkeypatch, [waiting(1, "2026-10-19T16:12:00Z")])
    loop.speed(store, DOCTOR, WATCHED, AFTER)
    [finding] = details(store)
    assert len(finding["evidence"]) == 2
    assert not any("during the GitHub incident" in line for line in finding["evidence"])


def test_a_crossing_cleared_in_the_next_window_retracts_the_previous_window_group(store, monkeypatch):  # noqa: F811
    status(monkeypatch)
    runs(monkeypatch, [waiting(1, "2026-10-19T16:40:00Z")])
    loop.speed(store, DOCTOR, WATCHED, AFTER - 60000)
    [finding] = details(store)
    started = waiting(1, "2026-10-19T16:40:00Z")
    started["jobs"][0].update(status="in_progress", started_at="2026-10-19T16:40:30Z", runner_name="r")
    runs(monkeypatch, [started])
    loop.speed(store, DOCTOR, WATCHED, AFTER)
    assert details(store) == []
    for texts in items(store).values():
        assert any(text.startswith("Retracted") and f"- {finding['id']}" in text for text in texts)


def test_hold_and_retraction_notices_reach_both_masters_as_information(store, monkeypatch):  # noqa: F811
    status(monkeypatch, INCIDENT)
    runs(monkeypatch, [])
    loop.speed(store, DOCTOR, WATCHED, DURING)
    status(monkeypatch)
    runs(monkeypatch, [waiting(1, "2026-10-19T16:40:00Z")])
    loop.speed(store, DOCTOR, WATCHED, AFTER - 60000)
    started = waiting(1, "2026-10-19T16:40:00Z")
    started["jobs"][0].update(status="in_progress", started_at="2026-10-19T16:40:30Z", runner_name="r")
    runs(monkeypatch, [started])
    loop.speed(store, DOCTOR, WATCHED, AFTER)
    for slug in (WATCHED, DOCTOR):
        notices = [
            item
            for item in InboxStore(store.redis).inbox(f"master@{slug}")
            if item.text.startswith(("Retracted", "Doctor CI speed findings"))
        ]
        assert len(notices) == 2
        assert all(item.fyi for item in notices)


def test_groups_label_retries_unassigned_runners_tiers_and_overlapping_incidents():
    start, end = iso_ms("2026-10-19T16:00:00Z"), iso_ms("2026-10-19T16:10:00Z")
    now, window = iso_ms("2026-10-19T16:30:00Z"), iso_ms("2026-10-19T16:20:00Z")
    incidents = [(start, end, "Actions delays"), (end, end + 60000, "Webhooks delays")]
    retry = {**waiting(1, "2026-10-19T16:10:00Z", labels=()), "run_attempt": 2}
    wall = {
        "id": 4,
        "event": "merge_group",
        "status": "completed",
        "conclusion": "success",
        "path": ".github/workflows/test.yml",
        "jobs": [
            {
                **waiting(4, "2026-10-19T16:00:00Z")["jobs"][0],
                "status": "in_progress",
                "started_at": "2026-10-19T15:51:00Z",
                "runner_name": "r",
            }
        ],
    }
    wall["jobs"][0]["completed_at"] = "2026-10-19T16:30:00Z"
    groups = ci_speed.group({}, [retry, wall], start, now, window, incidents)
    assert sorted(groups) == ["ci-queue-age-unconfirmed/[]", "ci-tier-wall-time/merge_group"]
    queued, tier = sorted(ci_speed.grouped(groups), key=lambda finding: finding.kind)
    assert queued.summary == "unassigned jobs read as queued up to 20.00 minutes in 1 run"
    assert queued.evidence[:2] == (
        "1 run, unassigned, worst 1200 seconds, median 1200 seconds, runs 1 attempt 2",
        "1 of these runs had jobs created during the GitHub incident Actions delays; Webhooks delays",
    )
    assert queued.threshold == "unconfirmed queue age over 15 minutes"
    assert tier.summary == "queue tier took up to 30.00 minutes in 1 run"
    assert tier.evidence[1] == "1 of these runs had jobs created during the GitHub incident Actions delays"
    assert tier.threshold == "tier wall time over 15 minutes"


def stalled_runs():
    runner = waiting(1, "2026-10-19T16:00:00Z")
    runner["jobs"][0].update(status="in_progress", started_at="2026-10-19T16:10:00Z", runner_name="r")
    runner.update(status="completed", conclusion="success")
    runner["jobs"][0]["completed_at"] = "2026-10-19T16:30:00Z"
    tier = {
        "id": 2,
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        "path": ".github/workflows/test.yml",
        "jobs": [{**runner["jobs"][0], "id": 20, "started_at": "2026-10-19T15:51:00Z"}],
    }
    return [runner, tier]


def test_two_windows_with_the_same_tier_and_labels_yield_one_finding_id():
    since, now = iso_ms("2026-10-19T16:00:00Z"), iso_ms("2026-10-19T16:30:00Z")
    first, second = now - 1200000, now - 600000
    ids = [
        sorted(finding.id for finding in ci_speed.grouped(ci_speed.group({}, stalled_runs(), since, now, window, [])))
        for window in (first, second)
    ]
    assert ids[0] == ids[1] == ['ci-runner-wait/["anton-oss", "self-hosted"]', "ci-tier-wall-time/pull_request"]


def test_a_later_window_replaces_the_group_it_keeps_for_the_same_id():
    since, now = iso_ms("2026-10-19T16:00:00Z"), iso_ms("2026-10-19T16:30:00Z")
    groups = ci_speed.group({}, stalled_runs(), since, now, now - 1200000, [])
    groups['ci-runner-wait/["anton-oss", "self-hosted"]']["members"]["stale"] = {"order": [0, 1]}
    ci_speed.group(groups, stalled_runs(), since, now, now - 600000, [])
    assert sorted(groups) == ['ci-runner-wait/["anton-oss", "self-hosted"]', "ci-tier-wall-time/pull_request"]
    assert {entry["window"] for entry in groups.values()} == {now - 600000}
    assert "stale" not in groups['ci-runner-wait/["anton-oss", "self-hosted"]']["members"]


@pytest.mark.parametrize(
    "judged,current,expected",
    [
        ('100-["a"]', '["a"]', []),
        ("100-merge_group", "merge_group", []),
        ('["a"]', '["a"]', []),
        ("merge_group", "pull_request", None),
        ('["a"]', '["a", "b"]', None),
    ],
)
def test_a_verdict_on_a_window_or_stable_id_covers_only_the_same_scope(store, judged, current, expected):  # noqa: F811
    from dataclasses import replace

    from scripts.swarm.health.findings import Finding

    kind = "ci tier wall time" if "_" in judged else "ci runner wait"
    original = Finding(kind, judged, "slow", ("text",), "wall time", 900, evidence_identity=("1", 900))
    loop.record(store, DOCTOR, [original], AFTER, 60000)
    judgments = loop.verdicts(store, DOCTOR)
    judgments.judge(original.id, "established", "investigated", "reader", AFTER)
    later = replace(original, subject=current)
    found = ci_speed.unjudged([later], store.redis.hgetall(judgments.key), AFTER + 1, 60000)
    assert found == ([later] if expected is None else expected)


def test_a_group_emptied_and_refilled_in_one_pass_is_not_retracted(store, monkeypatch):  # noqa: F811
    status(monkeypatch)
    first = waiting(1, "2026-10-19T16:40:00Z")
    records = [first]
    runs(monkeypatch, records)
    cli.timer(store, DOCTOR, AFTER + 60000, False)
    first["jobs"][0].update(status="in_progress", started_at="2026-10-19T16:41:00Z", runner_name="r")
    records.append(waiting(2, "2026-10-19T16:40:00Z"))
    cli.timer(store, DOCTOR, AFTER + 120000, False)
    identity = 'ci-queue-age-unconfirmed/["anton-oss", "self-hosted"]'
    assert [finding["id"] for finding in details(store)] == [identity]
    for texts in items(store).values():
        assert not any(text.startswith("Retracted") for text in texts)


def test_a_judged_open_crossing_does_not_return_as_its_clock_grows(store, monkeypatch):  # noqa: F811
    status(monkeypatch)
    job = {**waiting(4, "2026-10-19T16:40:00Z")["jobs"][0], "status": "in_progress", "runner_name": "r"}
    job["started_at"] = "2026-10-19T16:41:00Z"
    wall = {
        "id": 4,
        "event": "merge_group",
        "status": "in_progress",
        "path": ".github/workflows/test.yml",
        "jobs": [job],
    }
    runs(monkeypatch, [wall])
    for elapsed in [0, 60000, 660000]:
        cli.timer(store, DOCTOR, AFTER + elapsed, False)
        assert details(store) == []
        assert all(texts == [] for texts in items(store).values())
    job.update(status="completed", conclusion="success", completed_at="2026-10-19T17:15:00Z")
    wall.update(status="completed", conclusion="success")
    cli.timer(store, DOCTOR, AFTER + 1200000, False)
    [finding] = details(store)
    assert json.loads(store.redis.hget(loop.verdicts(store, DOCTOR).key, finding["id"]))["evidence_identity"] == [
        "4",
        2100,
    ]
    loop.verdicts(store, DOCTOR).judge(finding["id"], "established", "investigated", "reader", AFTER + 1200000)
    cli.timer(store, DOCTOR, AFTER + 1260000, False)
    cli.timer(store, DOCTOR, AFTER + 1860000, False)
    for texts in items(store).values():
        assert len(texts) == 1


def test_a_member_kept_before_open_ends_were_recorded_counts_as_settled():
    groups = {
        "ci-tier-wall-time/merge_group": {
            "kind": "ci tier wall time",
            "subject": "merge_group",
            "text": "queue",
            "window": 0,
            "members": {"a": {"order": [1, 1], "run": "1", "measure": 950, "evidence": "run 1", "during": []}},
        }
    }
    [finding] = ci_speed.grouped(groups)
    assert finding.evidence_identity == ("1", 950)


def wall_run(identity, event, status, gate_completed=None):
    job = {**waiting(identity, "2026-10-19T16:00:00Z")["jobs"][0], "status": "in_progress", "runner_name": "r"}
    job["started_at"] = "2026-10-19T16:00:00Z"
    job["completed_at"] = "2026-10-19T16:20:00Z" if status == "completed" else None
    gate = {**job, "id": identity * 10 + 1, "name": ci_speed.GATE, "completed_at": gate_completed}
    return {"id": identity, "event": event, "status": status, "conclusion": "success", "jobs": [job, gate]}


@pytest.mark.parametrize(
    "event,status,gate_completed,expected",
    [
        ("pull_request", "in_progress", None, False),
        ("pull_request", "in_progress", "2026-10-19T16:20:00Z", False),
        ("merge_group", "completed", None, True),
    ],
)
def test_a_wall_crossing_is_settled_and_exists_only_after_its_run_finishes(event, status, gate_completed, expected):
    since, now = iso_ms("2026-10-19T16:00:00Z"), iso_ms("2026-10-19T16:30:00Z")
    groups = ci_speed.group({}, [wall_run(5, event, status, gate_completed)], since, now, now - 600000, [])
    assert bool(groups) is expected
    if expected:
        [member] = groups[f"ci-tier-wall-time/{event}"]["members"].values()
        assert member["open"] is False


def test_a_window_start_is_dropped_from_a_dashed_label_set():
    assert ci_speed.scope_of('1792617600000-["anton-oss"]') == '["anton-oss"]'
    assert ci_speed.scope_of('["anton-oss"]') == '["anton-oss"]'
    assert ci_speed.scope_of("1792617600000-merge_group") == "merge_group"
