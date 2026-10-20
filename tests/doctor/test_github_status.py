import io
import json
import urllib.request

import pytest

from scripts.doctor import github_status
from scripts.swarm.ledger_events import iso_ms

pytestmark = pytest.mark.unit
GITHUB_STATUS_READ = True
NOW = iso_ms("2026-10-19T17:00:00Z")
DAY = 86_400_000


def page(monkeypatch, body):
    def urlopen(url, timeout=None):
        assert url == "https://www.githubstatus.com/api/v2/incidents/unresolved.json"
        assert timeout == 10
        if isinstance(body, Exception):
            raise body
        return io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)


def incident(identity, name, components=None, **fields):
    found = {"id": identity, "name": name, "created_at": "2026-10-19T15:50:00Z", **fields}
    if components is not None:
        found["components"] = [{"name": component} for component in components]
    return found


def test_unresolved_lists_only_actions_incidents_with_their_start_and_link(monkeypatch):
    page(
        monkeypatch,
        {
            "incidents": [
                incident("a", "Degraded Webhooks", ["Actions"], started_at="2026-10-19T15:49:00Z", shortlink="s/a"),
                incident("b", "Actions Job Delays"),
                incident("c", "Disruption with Pages", ["Pages"], started_at="2026-10-19T15:49:00Z"),
                incident("d", "Transactions are slow", []),
            ]
        },
    )
    assert github_status.unresolved() == [
        {"id": "a", "name": "Degraded Webhooks", "start": iso_ms("2026-10-19T15:49:00Z"), "link": "s/a"},
        {"id": "b", "name": "Actions Job Delays", "start": iso_ms("2026-10-19T15:50:00Z"), "link": ""},
    ]


@pytest.mark.parametrize("body", [OSError("down"), b"not json", {"page": {}}, {"incidents": [{"id": "x"}]}])
def test_an_unreadable_status_page_reads_as_none(monkeypatch, body):
    page(monkeypatch, body)
    assert github_status.unresolved() is None


def test_track_opens_listed_incidents_and_closes_those_no_longer_listed():
    known = {
        "open": {"id": "open", "name": "Open", "start": 1, "end": None, "noticed": True},
        "gone": {"id": "gone", "name": "Gone", "start": 2, "end": None, "noticed": True},
        "closed": {"id": "closed", "name": "Closed", "start": 3, "end": NOW - 1000, "noticed": True},
        "kept": {"id": "kept", "name": "Kept", "start": 4, "end": NOW - DAY + 1, "noticed": True},
        "old": {"id": "old", "name": "Old", "start": 5, "end": NOW - DAY, "noticed": True},
    }
    current = [
        {"id": "open", "name": "Open again", "start": 1, "link": "l"},
        {"id": "new", "name": "New", "start": 6, "link": ""},
    ]
    assert github_status.track(known, current, NOW) == {
        "open": {"id": "open", "name": "Open again", "start": 1, "end": None, "noticed": True, "link": "l"},
        "gone": {"id": "gone", "name": "Gone", "start": 2, "end": NOW, "noticed": True},
        "closed": {"id": "closed", "name": "Closed", "start": 3, "end": NOW - 1000, "noticed": True},
        "kept": {"id": "kept", "name": "Kept", "start": 4, "end": NOW - DAY + 1, "noticed": True},
        "new": {"id": "new", "name": "New", "start": 6, "end": None, "noticed": False, "link": ""},
    }
    assert known["gone"]["end"] is None


def test_track_without_a_read_changes_nothing_but_age():
    known = {
        "open": {"id": "open", "name": "Open", "start": 1, "end": None, "noticed": False},
        "old": {"id": "old", "name": "Old", "start": 2, "end": NOW - DAY, "noticed": True},
    }
    assert github_status.track(known, None, NOW) == {"open": known["open"]}


def test_windows_run_open_incidents_to_now():
    known = {
        "open": {"id": "open", "name": "Open", "start": 1, "end": None},
        "closed": {"id": "closed", "name": "Closed", "start": 2, "end": 3},
    }
    assert github_status.windows(known, NOW) == [(1, NOW, "Open"), (2, 3, "Closed")]
