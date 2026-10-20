import io
import json
import urllib.request
from datetime import datetime

import pytest

from scripts.doctor import ci_speed_read, cli
from scripts.inbox.store import InboxStore
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
    runs(monkeypatch, [waiting(n, "2026-10-19T16:01:00Z") for n in range(1, 21)])
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
    runs(monkeypatch, [waiting(n, "2026-10-19T16:50:00Z") for n in range(1, 6)])
    cli.timer(store, DOCTOR, AFTER, False)
    [finding] = details(store)
    assert finding["measure"] == 600
    assert "5 runs" in finding["evidence"][0]
    assert "5 of these runs had jobs created during the GitHub incident" in finding["evidence"][1]
    for texts in items(store).values():
        assert sum(text.startswith("New Doctor findings") for text in texts) == 1


def test_ten_runs_on_one_label_in_one_pass_are_one_finding(store, monkeypatch):  # noqa: F811
    status(monkeypatch)
    created = ["2026-10-19T16:53:00Z", "2026-10-19T16:54:00Z", "2026-10-19T16:52:00Z"] + ["2026-10-19T16:54:30Z"] * 7
    runs(monkeypatch, [waiting(n, stamp) for n, stamp in enumerate(created, 1)])
    actions = cli.timer(store, DOCTOR, AFTER, False)
    [finding] = details(store)
    assert finding["kind"] == "ci queue age unconfirmed"
    assert finding["summary"] == "anton-oss, self-hosted jobs read as queued up to 8.00 minutes in 10 runs"
    assert finding["evidence"][0] == (
        "10 runs, anton-oss, self-hosted, worst 480 seconds, median 330.0 seconds, runs 1, 2, 3, 4, 5, 6, 7, 8, 9, 10"
    )
    assert finding["measure"] == 480
    assert len(finding["evidence"]) == 11
    assert any("1 crossings" in action for action in actions)
    for texts in items(store).values():
        [text] = texts
        assert text.count("\n- ") == 1


def test_groups_keep_label_sets_and_tiers_apart_and_grow_without_another_notice(store, monkeypatch):  # noqa: F811
    status(monkeypatch)
    batch = waiting(3, "2026-10-19T16:50:00Z", labels=["anton-batch"])
    wall = {
        "id": 4,
        "event": "merge_group",
        "status": "in_progress",
        "path": ".github/workflows/test.yml",
        "jobs": [{**waiting(4, "2026-10-19T16:40:00Z")["jobs"][0], "status": "in_progress", "runner_name": "r"}],
    }
    wall["jobs"][0]["started_at"] = "2026-10-19T16:41:00Z"
    runs(monkeypatch, [waiting(1, "2026-10-19T16:50:00Z"), batch, wall])
    cli.timer(store, DOCTOR, AFTER, False)
    assert sorted(finding["kind"] for finding in details(store)) == [
        "ci queue age unconfirmed",
        "ci queue age unconfirmed",
        "ci tier wall time",
    ]
    [tier] = [finding for finding in details(store) if finding["kind"] == "ci tier wall time"]
    assert tier["summary"] == "queue tier took up to 20.00 minutes in 1 runs"
    runs(monkeypatch, [waiting(2, "2026-10-19T16:52:00Z")])
    cli.timer(store, DOCTOR, AFTER + 60000, False)
    [grown] = [f for f in details(store) if "anton-oss" in f["summary"]]
    assert "2 runs" in grown["evidence"][0] and "runs 1, 2" in grown["evidence"][0]
    for texts in items(store).values():
        assert len(texts) == 1


def test_a_status_page_read_failure_never_holds_findings(store, monkeypatch):  # noqa: F811
    status(monkeypatch, down=True)
    runs(monkeypatch, [waiting(n, "2026-10-19T16:01:00Z") for n in range(1, 3)])
    actions = cli.timer(store, DOCTOR, DURING, False)
    [finding] = details(store)
    assert "2 runs" in finding["evidence"][0]
    assert not any("held" in action for action in actions)
    for texts in items(store).values():
        [text] = texts
        assert text.startswith("New Doctor findings")


def test_an_incident_outside_actions_does_not_hold_findings(store, monkeypatch):  # noqa: F811
    status(monkeypatch, PAGES)
    runs(monkeypatch, [waiting(1, "2026-10-19T16:01:00Z")])
    cli.timer(store, DOCTOR, DURING, False)
    assert len(details(store)) == 1
    for texts in items(store).values():
        [text] = texts
        assert text.startswith("New Doctor findings")


def test_an_incident_named_for_actions_without_components_holds_findings(store, monkeypatch):  # noqa: F811
    status(monkeypatch, {**INCIDENT, "name": "Actions Job Delays", "components": []})
    runs(monkeypatch, [waiting(1, "2026-10-19T16:01:00Z")])
    cli.timer(store, DOCTOR, DURING, False)
    assert details(store) == []
