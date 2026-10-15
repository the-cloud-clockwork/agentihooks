import json
import subprocess

import pytest

from scripts import ci_requeue
from scripts.swarm.store import SwarmError

pytestmark = pytest.mark.unit


def _pull(number, created, *, draft=False, queued=False, rollup="SUCCESS"):
    commits = [] if rollup is None else [{"commit": {"statusCheckRollup": rollup and {"state": rollup}}}]
    return {
        "number": number,
        "url": f"https://github.com/o/r/pull/{number}",
        "isDraft": draft,
        "createdAt": created,
        "mergeQueueEntry": {"id": f"entry{number}"} if queued else None,
        "commits": {"nodes": commits},
    }


def _page(nodes, cursor=None):
    return {
        "data": {
            "repository": {
                "pullRequests": {"pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}, "nodes": nodes}
            }
        }
    }


class Fake:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def __call__(self, command, **_):
        fields = dict(part.split("=", 1) for part in command[command.index("graphql") + 1 :] if "=" in part)
        self.calls.append(fields)
        page = self.pages[fields.get("after")]
        return subprocess.CompletedProcess(command, 0, json.dumps(page), "")


def test_only_open_green_ready_pull_requests_outside_the_queue_are_candidates():
    fake = Fake(
        {
            None: _page(
                [
                    _pull(1, "2026-10-15T10:00:00Z"),
                    _pull(2, "2026-10-15T09:00:00Z", draft=True),
                    _pull(3, "2026-10-15T08:00:00Z", queued=True),
                    _pull(4, "2026-10-15T07:00:00Z", rollup="FAILURE"),
                    _pull(5, "2026-10-15T06:00:00Z", rollup="PENDING"),
                    _pull(6, "2026-10-15T05:00:00Z", rollup=""),
                    _pull(7, "2026-10-15T04:00:00Z", rollup=None),
                ]
            )
        }
    )
    assert [pull["number"] for pull in ci_requeue.candidates("o/r", fake)] == [1]
    assert fake.calls[0]["owner"] == "o" and fake.calls[0]["name"] == "r"
    assert 'baseRefName: "dev"' in fake.calls[0]["query"] and "states: OPEN" in fake.calls[0]["query"]


def test_every_page_is_read_and_candidates_come_oldest_first():
    fake = Fake(
        {
            None: _page([_pull(9, "2026-10-15T12:00:00Z"), _pull(8, "2026-10-15T11:00:00Z")], cursor="c1"),
            "c1": _page([_pull(3, "2026-10-14T23:00:00Z")]),
        }
    )
    assert [pull["number"] for pull in ci_requeue.candidates("o/r", fake)] == [3, 8, 9]
    assert [call.get("after") for call in fake.calls] == [None, "c1"]


def test_the_sweep_queues_oldest_first_and_keeps_going_past_a_refusal(monkeypatch):
    pulls = [_pull(1, "2026-10-15T01:00:00Z"), _pull(2, "2026-10-15T02:00:00Z"), _pull(3, "2026-10-15T03:00:00Z")]
    monkeypatch.setattr(ci_requeue, "candidates", lambda repo, run: pulls)
    seen = []

    def operate(action, url, run):
        seen.append((action, url))
        if url.endswith("/1"):
            return {"queued": True, "state": "OPEN"}
        if url.endswith("/2"):
            raise SwarmError("the current pull request head must pass Tests before queueing")
        return {"queued": False, "state": "OPEN", "waiting": "checks"}

    monkeypatch.setattr(ci_requeue.merge_queue, "operate", operate)
    rows = ci_requeue.sweep("o/r", run=None)
    assert seen == [("queue", pull["url"]) for pull in pulls]
    assert [row["outcome"] for row in rows] == [
        "queued",
        "refused: the current pull request head must pass Tests before queueing",
        "waiting",
    ]


def test_a_repository_without_a_queue_reports_the_merge(monkeypatch):
    monkeypatch.setattr(ci_requeue, "candidates", lambda repo, run: [_pull(4, "2026-10-15T01:00:00Z")])
    monkeypatch.setattr(
        ci_requeue.merge_queue, "operate", lambda action, url, run: {"queued": False, "state": "MERGED"}
    )
    assert [row["outcome"] for row in ci_requeue.sweep("o/r", run=None)] == ["merged"]


def test_main_prints_the_list(monkeypatch, capsys):
    rows = [
        {"number": 7, "created": "2026-10-15T01:00:00Z", "outcome": "queued", "url": "https://github.com/o/r/pull/7"}
    ]
    monkeypatch.setattr(ci_requeue, "sweep", lambda repo: rows if repo == "o/r" else [])
    assert ci_requeue.main(["--repo", "o/r"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "7 2026-10-15T01:00:00Z queued https://github.com/o/r/pull/7",
        "1 pull requests swept",
    ]
