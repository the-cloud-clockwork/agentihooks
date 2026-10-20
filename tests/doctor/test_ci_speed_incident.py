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


def test_a_wait_across_a_window_boundary_is_one_crossing_per_pass(store, monkeypatch):  # noqa: F811
    status(monkeypatch)
    runs(monkeypatch, [waiting(1, "2026-10-19T16:40:00Z")])
    before = cli.timer(store, DOCTOR, AFTER - 60000, False)
    after = cli.timer(store, DOCTOR, AFTER, False)
    assert any("1 crossings" in action for action in before)
    assert any("1 crossings" in action for action in after)
    assert len(details(store)) == 2
    for texts in items(store).values():
        assert len(texts) == 2


def test_groups_keep_label_sets_and_tiers_apart_and_grow_without_another_notice(store, monkeypatch):  # noqa: F811
    status(monkeypatch)
    batch = waiting(3, "2026-10-19T16:40:00Z", labels=["anton-batch"])
    wall = {
        "id": 4,
        "event": "merge_group",
        "status": "in_progress",
        "path": ".github/workflows/test.yml",
        "jobs": [{**waiting(4, "2026-10-19T16:40:00Z")["jobs"][0], "status": "in_progress", "runner_name": "r"}],
    }
    wall["jobs"][0]["started_at"] = "2026-10-19T16:41:00Z"
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


def test_groups_label_retries_unassigned_runners_tiers_and_overlapping_incidents():
    start, end = iso_ms("2026-10-19T16:00:00Z"), iso_ms("2026-10-19T16:10:00Z")
    now, window = iso_ms("2026-10-19T16:30:00Z"), iso_ms("2026-10-19T16:20:00Z")
    incidents = [(start, end, "Actions delays"), (end, end + 60000, "Webhooks delays")]
    retry = {**waiting(1, "2026-10-19T16:10:00Z", labels=()), "run_attempt": 2}
    wall = {
        "id": 4,
        "event": "merge_group",
        "status": "in_progress",
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
    groups = ci_speed.group({}, [retry, wall], start, now, window, incidents)
    assert sorted(groups) == [f"ci-queue-age-unconfirmed/{window}-[]", f"ci-tier-wall-time/{window}-merge_group"]
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
