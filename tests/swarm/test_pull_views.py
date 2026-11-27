import contextvars
import json
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm.ledger_events import GATE, ROLLUP_FALLBACK, SENT_TTL_S, PullRequest
from scripts.swarm.store import PREFIX
from tests.swarm.graph_port import view as single_view
from tests.swarm.graph_port import views


def resource(head="h1", state="OPEN"):
    return {
        "state": state,
        "mergedAt": None,
        "headRefOid": head,
        "mergeQueueEntry": None,
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "committedDate": "2026-10-08T21:00:00Z",
                        "file": None,
                        "checkSuites": {"nodes": [], "pageInfo": {}},
                        "statusCheckRollup": None,
                    }
                }
            ]
        },
    }


def test_distinct_pull_requests_share_a_bounded_graphql_request():
    urls = [f"https://github.com/owner/repo/pull/{n}" for n in range(21)]
    urls[1] = "https://github.com/other/repository/pull/2"
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        count = command[-1].count("resource(url:")
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"data": {f"p{i}": resource(f"h{i}") for i in range(count)}})
        )

    found = views(urls + urls, run)
    calls.sort(key=lambda call: -call[0][-1].count("resource(url:"))
    assert len(calls) == 2
    assert len(found) == 21
    assert found[urls[1]].head == "h1"
    assert found[urls[20]].head == "h0"
    query = calls[0][0][-1]
    assert calls[0][0][:4] == ["gh", "api", "graphql", "-f"]
    assert query.startswith("query={p0:resource(url:") and query.endswith("}")
    assert query.count("{") == query.count("}")
    assert "} p1:resource(url:" in query
    assert ' file(path:".github/workflows"){object{id}} statusCheckRollup{' in query
    assert urls[0] in query and urls[1] in query
    assert query.count("resource(url:") == 20
    assert calls[0][1] == {"capture_output": True, "text": True, "timeout": 20}


def test_a_bad_resource_does_not_discard_its_neighbors():
    urls = [f"https://github.com/o/r/pull/{n}" for n in range(4)]
    paginated = resource()
    paginated["commits"]["nodes"][0]["commit"]["checkSuites"]["pageInfo"] = {"hasNextPage": True}
    data = {"p0": resource(), "p1": None, "p2": paginated, "p3": {"state": "OPEN"}}
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"data": data}))

    found = views(urls, run)
    assert len(calls) == 1
    assert found[urls[0]].head == "h1"
    assert [found[url] for url in urls[1:]] == [None, None, None]


def test_a_batched_pull_request_pages_through_more_than_one_hundred_contexts():
    urls = [f"https://github.com/o/r/pull/{n}" for n in range(2)]
    big = resource("h2")
    commit = big["commits"]["nodes"][0]["commit"]
    commit["id"] = "C_h2"
    first = [{"name": f"job {n}", "conclusion": "SUCCESS", "databaseId": n} for n in range(100)]
    commit["statusCheckRollup"] = {"contexts": {"nodes": first, "pageInfo": {"hasNextPage": True, "endCursor": "c"}}}
    rest = [{"name": "job 100", "conclusion": "FAILURE", "databaseId": 100}]
    page = {"node": {"statusCheckRollup": {"contexts": {"nodes": rest, "pageInfo": {"hasNextPage": False}}}}}
    replies = [{"data": {"p0": resource(), "p1": big}}, {"data": page}]
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps(replies[len(calls) - 1]))

    found = views(urls, run)
    assert len(calls) == 2
    assert {"id=C_h2", "after=c"} <= set(calls[1])
    assert "isRequired(pullRequestNumber:1)" in calls[1][4]
    assert "isRequired(pullRequestNumber:0)" in calls[0][-1] and "$number" not in calls[0][-1]
    assert found[urls[0]].head == "h1"
    assert found[urls[1]].failed == ("job 100",)
    assert found[urls[1]].red is True


def test_partial_graphql_errors_leave_other_pull_requests_readable():
    urls = [f"https://github.com/o/r/pull/{n}" for n in range(2)]
    raw = {
        "data": {"p0": resource(), "p1": resource()},
        "errors": [{"path": ["p1", "commits"], "message": "unavailable"}],
    }
    found = views(urls, lambda *a, **k: SimpleNamespace(returncode=1, stdout=json.dumps(raw)))
    assert found[urls[0]].head == "h1"
    assert found[urls[1]] is None


def test_a_global_graphql_error_discards_the_batch():
    url = "https://github.com/o/r/pull/1"
    raw = {"data": {"p0": resource()}, "errors": [{"message": "rate limited"}]}
    assert views([url], lambda *a, **k: SimpleNamespace(returncode=1, stdout=json.dumps(raw))) == {url: None}


@pytest.mark.parametrize("failure", ["returncode", "json", "timeout", "missing"])
def test_failed_batches_stay_unknown(failure):
    url = "https://github.com/o/r/pull/1"

    def run(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("gh", 20)
        return SimpleNamespace(returncode=int(failure == "returncode"), stdout="bad" if failure == "json" else "{}")

    assert views([url], run) == {url: None}


def test_each_batch_fetch_observes_the_new_head_and_merge():
    url = "https://github.com/o/r/pull/1"
    responses = iter([resource(), resource("h2", "MERGED")])

    def run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"data": {"p0": next(responses)}}))

    assert views([url], run)[url].head == "h1"
    changed = views([url], run)[url]
    assert (changed.head, changed.state) == ("h2", "MERGED")


def test_tick_rollups_share_reads_until_expiry_but_merge_reads_stay_fresh(monkeypatch, tmp_path):
    import time

    import fakeredis

    from scripts.swarm import github_reads

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    clock = [1_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    url = "https://github.com/o/r/pull/1"
    cache = fakeredis.FakeRedis(decode_responses=True)
    current = [resource()]
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        data = {"p0": current[0]} if "p0:" in command[-1] else {"resource": current[0]}
        return SimpleNamespace(returncode=0, stdout=json.dumps({"data": data}))

    assert views([url], run, cache)[url].head == "h1"
    current[0] = resource("h2", "MERGED")
    assert views([url], run, cache)[url].head == "h1"
    assert len(calls) == 1
    assert single_view(url, run).state == "MERGED"
    assert len(calls) == 2
    clock[0] += github_reads.RUN_TTL_S
    assert views([url], run, cache)[url].head == "h2"
    assert len(calls) == 3
    assert github_reads.counts(0, clock[0] + 1) == {"swarm.checks": 2, "swarm.checks.fresh": 1}

    rows = [json.loads(line) for line in (github_reads.home() / "charged.jsonl").read_text().splitlines()]
    assert [row["endpoint"] for row in rows] == ["graphql"] * 3


def test_shared_rollup_snapshots_are_kept_apart_per_query(monkeypatch, tmp_path):
    import fakeredis

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    first, second = "https://github.com/o/r/pull/1", "https://github.com/o/r/pull/2"
    cache = fakeredis.FakeRedis(decode_responses=True)
    heads = {json.dumps(first): "h1", json.dumps(second): "h2"}

    def run(command, **kwargs):
        head = next(head for key, head in heads.items() if key in command[-1])
        return SimpleNamespace(returncode=0, stdout=json.dumps({"data": {"p0": resource(head)}}))

    assert views([first], run, cache)[first].head == "h1"
    assert views([second], run, cache)[second].head == "h2"


def test_unavailable_shared_snapshot_storage_keeps_live_rollups_readable(monkeypatch, tmp_path):
    import fakeredis

    unavailable = tmp_path / "file"
    unavailable.touch()
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(unavailable))
    url = "https://github.com/o/r/pull/1"
    cache = fakeredis.FakeRedis(decode_responses=True)
    responses = iter([resource(), resource("h2", "MERGED")])

    def run(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"data": {"p0": next(responses)}}))

    assert views([url], run, cache)[url].head == "h1"
    assert views([url], run, cache)[url].head == "h2"


def test_empty_batch_never_launches_a_command():
    assert views([], lambda *a, **k: pytest.fail("empty request")) == {}


def test_workflow_declarations_cache_by_immutable_tree_while_heads_and_checks_refresh(monkeypatch):
    import fakeredis

    cache = fakeredis.FakeRedis(decode_responses=True)
    url = "https://github.com/o/r/pull/1"
    calls = []

    def run(command, **kwargs):
        query = command[-1]
        calls.append(query)
        if "nodes(ids:" in query:
            tree = "tree1" if '"tree1"' in query else "tree2"
            assert (
                query
                == f'query={{nodes(ids:["{tree}"]){{id ...on Tree{{entries{{name object{{...on Blob{{text}}}}}}}}}}}}'
            )
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "data": {
                            "nodes": [
                                {
                                    "id": tree,
                                    "entries": [{"object": {"text": f"  gate:\n    name: {GATE}"}}]
                                    if tree == "tree1"
                                    else [],
                                }
                            ]
                        }
                    }
                ),
            )
        pr = resource(f"h{len(calls)}")
        commit = pr["commits"]["nodes"][0]["commit"]
        commit["file"] = {"object": {"id": "tree1" if len(calls) < 4 else "tree2"}}
        commit["statusCheckRollup"] = {"contexts": {"nodes": [{"name": GATE, "conclusion": "SUCCESS"}], "pageInfo": {}}}
        return SimpleNamespace(returncode=0, stdout=json.dumps({"data": {"p0": pr}}))

    import time

    clock = [1_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    first = views([url], run, cache)[url]
    clock[0] += 60
    second = views([url], run, cache)[url]
    clock[0] += 60
    third = views([url], run, cache)[url]
    clock[0] += 60
    fourth = views([url], run, cache)[url]
    assert len(calls) == 6
    assert fourth.gate_passed is False
    assert first.head != second.head
    assert first.gate_passed and second.gate_passed
    assert not third.gate_passed
    assert "Blob{text}" not in calls[0]
    assert all(key.startswith(f"{PREFIX}:") for key in cache.scan_iter())
    assert 0 < cache.ttl(f"{PREFIX}:workflow-gate-files:tree1") <= SENT_TTL_S


@pytest.mark.parametrize("batched", [False, True])
@pytest.mark.parametrize("optional", [False, True])
@pytest.mark.parametrize("conclusion", [None, "FAILURE", "SUCCESS"])
@pytest.mark.parametrize("phase", ["jobs", "suite", "absent"])
@pytest.mark.parametrize("event", [None, "pull_request"])
def test_a_status_gate_judges_only_jobs_of_its_declared_workflow(batched, optional, conclusion, phase, event):
    import fakeredis

    url = "https://github.com/o/r/pull/1"
    required = {"id": "W1", "resourcePath": "/o/r/actions/workflows/test.yml"}
    other = {"id": "W2", "resourcePath": "/o/r/actions/workflows/docs.yml"}
    run = {"databaseId": 1, "event": event, "workflow": required}
    probe = {"databaseId": 2 if optional else 1, "event": event, "workflow": other if optional else required}
    checks = [
        {"context": GATE, "state": "SUCCESS", "isRequired": True},
        {
            "name": "unit",
            "databaseId": 10,
            "conclusion": "SUCCESS",
            "isRequired": False,
            "checkSuite": {"workflowRun": run},
        },
        {
            "name": "probe",
            "databaseId": 20,
            "conclusion": conclusion,
            "isRequired": False,
            "checkSuite": {"workflowRun": probe},
        },
    ]
    suites = [
        {"status": "COMPLETED", "workflowRun": run},
        {"status": "IN_PROGRESS" if conclusion is None else "COMPLETED", "workflowRun": probe},
    ]
    if phase != "jobs":
        checks = [checks[0], *([checks[-1]] if optional else [])]
    if phase == "absent":
        suites = [suites[-1]] if optional else []
    tree = {
        "id": "tree1",
        "entries": [
            {"name": "test.yml", "object": {"text": f"name: Tests\njobs:\n  gate:\n    name: {GATE}\n"}},
            {"name": "docs.yml", "object": {"text": "name: Docs\njobs:\n  measure:\n    name: probe\n"}},
        ],
    }
    raw = resource()
    commit = raw["commits"]["nodes"][0]["commit"]
    commit["file"] = {"object": tree}
    commit["statusCheckRollup"] = {"contexts": {"nodes": checks, "pageInfo": {}}}
    commit["checkSuites"] = {"nodes": suites, "pageInfo": {}}
    calls = []

    def read(command, **kwargs):
        query = next(arg for arg in command if arg.startswith("query="))
        calls.append(query)
        if "nodes(ids:" in query:
            assert "entries{name object" in query
            data = {"nodes": [tree]}
        else:
            assert "workflow{id name resourcePath}" in query
            assert "workflow{id resourcePath}" in query
            snapshot = json.loads(json.dumps(raw))
            if batched:
                snapshot["commits"]["nodes"][0]["commit"]["file"] = {"object": {"id": "tree1"}}
            data = {"p0" if batched else "resource": snapshot}
        return SimpleNamespace(returncode=0, stdout=json.dumps({"data": data}))

    cache = fakeredis.FakeRedis(decode_responses=True)
    for _ in range(2):
        pull = views([url], read, cache)[url] if batched else single_view(url, read)
        assert pull.head == "h1"
        assert pull.resolved is (phase == "jobs" and (optional or conclusion is not None))
        assert pull.red is (phase == "jobs" and not optional and conclusion == "FAILURE")
        assert pull.failed == (("probe",) if phase == "jobs" and not optional and conclusion == "FAILURE" else ())
    assert sum("nodes(ids:" in query for query in calls) == int(batched)


def test_unreadable_workflow_declaration_is_unknown_and_is_not_cached():
    import fakeredis

    cache = fakeredis.FakeRedis(decode_responses=True)
    url = "https://github.com/o/r/pull/1"
    pr = resource()
    pr["commits"]["nodes"][0]["commit"]["file"] = {"object": {"id": "tree1"}}
    calls = []

    def run(command, **kwargs):
        calls.append(command[-1])
        data = {"nodes": [None]} if "nodes(ids:" in command[-1] else {"p0": pr}
        return SimpleNamespace(returncode=0, stdout=json.dumps({"data": data}))

    assert views([url], run, cache) == {url: None}
    assert views([url], run, cache) == {url: None}
    assert len(calls) == 3
    assert cache.dbsize() == 0


@pytest.mark.parametrize(
    "doc", [{}, {"tasks": [{"state": "claimed", "pr_url": "active"}, {"state": "done", "pr_url": "old"}]}]
)
def test_tick_snapshot_batches_claimed_tasks_and_caches_fallbacks(doc, monkeypatch):
    import fakeredis

    from scripts.inbox.store import InboxStore
    from scripts.swarm import ledger_events
    from scripts.swarm.store import RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    inbox = InboxStore(store.redis)
    item = inbox.send("swarm", "eng-1@sw", "notice")
    inbox.close(item.id, "swarm", "done", "settled")
    store.redis.hset(store.key("sw", "red-notices"), item.id, "closed-notice")
    batched, fallback = [], []
    monkeypatch.setattr(
        ledger_events, "views", lambda urls, cache=None, ttl=None: batched.append(urls) or {url: None for url in urls}
    )
    monkeypatch.setattr(ledger_events, "view", lambda url: fallback.append(url))
    lookup = ledger_events.tick_view(inbox, store, "sw", doc)
    assert batched == [["active"]] if doc else batched == [[]]
    if doc:
        assert lookup("active") is None
        assert fallback == []
    lookup("other")
    lookup("other")
    assert fallback == ["other"]


@pytest.mark.feature("tick-view-pull-batch-cache-ttl")
def test_tick_view_batches_claimed_pull_requests_with_the_tick_view_ttl(monkeypatch):
    import fakeredis

    from scripts.inbox.store import InboxStore
    from scripts.swarm import ledger_events
    from scripts.swarm.store import RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    inbox = InboxStore(store.redis)
    doc = {"tasks": [{"id": "t1", "state": "pr", "pr_url": "a"}]}
    ttls = []
    monkeypatch.setattr(
        ledger_events, "views", lambda urls, cache=None, ttl=None: ttls.append(ttl) or dict.fromkeys(urls)
    )
    ledger_events.tick_view(inbox, store, "sw", doc)
    assert ttls == [ledger_events.TICK_VIEW_TTL_S]


def test_a_later_tick_snapshot_fetches_only_pull_requests_the_earlier_one_did_not(monkeypatch):
    import fakeredis

    from scripts.inbox.store import InboxStore
    from scripts.swarm import ledger_events
    from scripts.swarm.store import RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    inbox = InboxStore(store.redis)
    batched, fallback = [], []
    monkeypatch.setattr(
        ledger_events,
        "views",
        lambda urls, cache=None, ttl=None: batched.append(urls) or {url: f"fresh {url}" for url in urls},
    )
    monkeypatch.setattr(ledger_events, "view", lambda url: fallback.append(url))
    first = ledger_events.tick_view(inbox, store, "sw", {"tasks": [{"state": "pr", "pr_url": "a"}]})
    tasks = [{"state": "pr", "pr_url": "a"}, {"state": "claimed", "pr_url": "b"}]
    later = ledger_events.tick_view(inbox, store, "sw", {"tasks": tasks}, first)
    assert batched == [["a"], ["b"]]
    assert (later("a"), later("b")) == ("fresh a", "fresh b")
    assert fallback == []


def test_a_later_tick_snapshot_retries_a_pull_request_the_earlier_one_failed_to_read(monkeypatch):
    import fakeredis

    from scripts.inbox.store import InboxStore
    from scripts.swarm import ledger_events
    from scripts.swarm.store import RedisStore

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    inbox = InboxStore(store.redis)
    doc = {"tasks": [{"state": "pr", "pr_url": "a"}]}
    monkeypatch.setattr(ledger_events, "views", lambda urls, cache=None, ttl=None: dict.fromkeys(urls))
    first = ledger_events.tick_view(inbox, store, "sw", doc)
    batched = []
    monkeypatch.setattr(
        ledger_events,
        "views",
        lambda urls, cache=None, ttl=None: batched.append(urls) or {url: f"fresh {url}" for url in urls},
    )
    later = ledger_events.tick_view(inbox, store, "sw", doc, first)
    assert batched == [["a"]]
    assert later("a") == "fresh a"


def test_metrics_completed_and_split_pulls_are_prefetched_without_late_slow_reads(monkeypatch, tmp_path):
    import time

    import fakeredis

    from scripts.inbox.store import InboxStore
    from scripts.swarm import ledger_events, metrics_outbox, metrics_swarm
    from scripts.swarm.store import RedisStore

    urls = [f"https://github.com/o/r/pull/{number}" for number in range(1, 5)]
    doc = {
        "tasks": [
            {"id": "active", "state": "pr", "pr_url": urls[0], "split_pulls": [urls[1]]},
            {"id": "done", "state": "done", "pr_url": urls[2], "split_pulls": [urls[3]]},
        ],
        "_meta": {"events": [{"kind": "task done", "target": "tasks/done", "at": int(time.time() * 1000)}]},
    }
    elapsed = [0.0]
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))

    def batch(found, cache=None, ttl=None):
        elapsed[0] += 20
        return dict.fromkeys(found)

    def late(url):
        elapsed[0] += 20
        return None

    monkeypatch.setattr(ledger_events, "views", batch)
    monkeypatch.setattr(ledger_events, "view", late)
    lookup = ledger_events.tick_view(InboxStore(store.redis), store, "sw", doc)
    box = metrics_outbox.Outbox(tmp_path / "metrics.sqlite", metrics_outbox.Settings("http://sink", "", ""))
    try:
        started = elapsed[0]
        found = metrics_swarm.pull_rows(
            box, int(time.time() * 1000), metrics_swarm.TickInput(store, doc, [], lookup, None)
        )
        assert elapsed[0] - started < 30, f"Late PR reads held metrics for {elapsed[0] - started} seconds"
        assert set(found) == set(urls)
    finally:
        box.close()


@pytest.mark.parametrize(
    "recorded_primary,recorded_split", [(True, True), (True, False), (False, True), (False, False)]
)
def test_tick_prefetch_skips_recorded_completed_metrics_but_keeps_unrecorded_backfill(
    monkeypatch, tmp_path, recorded_primary, recorded_split
):
    import time

    import fakeredis

    from scripts.inbox.store import InboxStore
    from scripts.swarm import ledger_events, metrics_outbox
    from scripts.swarm.store import RedisStore

    now = int(time.time() * 1000)
    urls = [f"https://github.com/o/r/pull/{number}" for number in range(1, 5)]
    doc = {
        "tasks": [
            {"id": "active", "state": "pr", "pr_url": urls[0], "split_pulls": [urls[1]]},
            {"id": "done", "state": "done", "pr_url": urls[2], "split_pulls": [urls[3]]},
        ],
        "_meta": {"events": [{"kind": "task done", "target": "tasks/done", "at": now}]},
    }
    path = tmp_path / "metrics.sqlite"
    monkeypatch.setattr(metrics_outbox, "spool_path", lambda: path)
    table = metrics_outbox.Table("delivery_events", (("kind", "String"), ("pull_request", "String")))
    box = metrics_outbox.Outbox(path, metrics_outbox.Settings("http://sink", "", ""))
    try:
        for enabled, kind, url in [
            (recorded_primary, "merge", urls[2]),
            (recorded_split, "pull_request_merged", urls[3]),
        ]:
            if enabled:
                box.append(
                    table,
                    [
                        {
                            "event_id": kind,
                            "ledger": "sw",
                            "plan": "",
                            "phase": "",
                            "slice": "",
                            "task": "",
                            "ts_ms": now,
                            "kind": kind,
                            "pull_request": url,
                        }
                    ],
                )
    finally:
        box.close()
    batches = []
    monkeypatch.setattr(
        ledger_events, "views", lambda found, cache=None, ttl=None: batches.append(found) or dict.fromkeys(found)
    )
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))

    ledger_events.tick_view(InboxStore(store.redis), store, "sw", doc)
    ledger_events.tick_view(InboxStore(store.redis), store, "sw", doc)

    expected = (
        {urls[0], urls[1]}
        | ({urls[2]} if not recorded_primary else set())
        | ({urls[3]} if not recorded_split else set())
    )
    assert len(batches) == 2
    assert all(set(batch) == expected for batch in batches)


def test_expired_metric_rows_do_not_hide_recent_completion_backfill(monkeypatch, tmp_path):
    import time

    import fakeredis

    from scripts.inbox.store import InboxStore
    from scripts.swarm import ledger_events, metrics_outbox
    from scripts.swarm.store import RedisStore

    now, url = int(time.time() * 1000), "https://github.com/o/r/pull/1"
    path = tmp_path / "metrics.sqlite"
    monkeypatch.setattr(metrics_outbox, "spool_path", lambda: path)
    table = metrics_outbox.Table("delivery_events", (("kind", "String"), ("pull_request", "String")))
    box = metrics_outbox.Outbox(path, metrics_outbox.Settings("http://sink", "", ""))
    try:
        box.append(
            table,
            [
                {
                    "event_id": "old",
                    "ledger": "sw",
                    "plan": "",
                    "phase": "",
                    "slice": "",
                    "task": "",
                    "ts_ms": now - metrics_outbox.DAY_MS - 1,
                    "kind": "merge",
                    "pull_request": url,
                }
            ],
        )
    finally:
        box.close()
    doc = {
        "tasks": [{"id": "done", "state": "done", "pr_url": url}],
        "_meta": {"events": [{"kind": "task done", "target": "tasks/done", "at": now}]},
    }
    batches = []
    monkeypatch.setattr(
        ledger_events, "views", lambda found, cache=None, ttl=None: batches.append(found) or dict.fromkeys(found)
    )
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    ledger_events.tick_view(InboxStore(store.redis), store, "sw", doc)
    assert batches == [[url]]


def test_missing_workflow_directory_does_not_declare_a_required_gate():
    url = "https://github.com/o/r/pull/1"
    pr = resource()
    pr["commits"]["nodes"][0]["commit"]["statusCheckRollup"] = {
        "contexts": {"nodes": [{"name": GATE, "conclusion": "SUCCESS"}], "pageInfo": {}}
    }
    found = views([url], lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps({"data": {"p0": pr}})))[url]
    assert found.gate_passed is False


def test_an_unreadable_workflow_tree_does_not_hide_the_readable_tree_after_it():
    urls = ["https://github.com/o/r/pull/1", "https://github.com/o/r/pull/2"]
    data = {"p0": resource(), "p1": resource()}
    for i in range(2):
        data[f"p{i}"]["commits"]["nodes"][0]["commit"]["file"] = {"object": {"id": f"tree{i}"}}

    def run(command, **kwargs):
        answer = {"nodes": [None, {"id": "tree1", "entries": []}]} if "nodes(ids:" in command[-1] else data
        return SimpleNamespace(returncode=0, stdout=json.dumps({"data": answer}))

    found = views(urls, run)
    assert found[urls[0]] is None
    assert found[urls[1]].head == "h1"


ROLLUP_PATH = ["resource", "commits", "nodes", 0, "commit", "statusCheckRollup"]


def _refused(state, errors):
    raw = resource(state=state)
    raw["mergedAt"] = "2026-10-21T10:00:00Z" if state == "MERGED" else None
    stdout = json.dumps({"data": {"resource": raw}, "errors": errors})
    return lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=stdout, stderr="")


@pytest.mark.parametrize("field", ["statusCheckRollup", "checkSuites"])
def test_a_merged_pull_request_with_unreadable_checks_reads_merged_unresolved_and_names_the_fallback(field):
    message = "Resource not accessible by integration"
    forbidden = [
        {"type": "FORBIDDEN", "path": [*ROLLUP_PATH[:-1], field], "message": message},
        {"type": "FORBIDDEN", "path": [*ROLLUP_PATH, "contexts", "nodes", 0, "checkSuite"], "message": message},
    ]
    pull = single_view("https://github.com/o/r/pull/9", _refused("MERGED", forbidden))
    assert pull == PullRequest("MERGED", 1_792_576_800_000, None, False, head="h1", fallback=ROLLUP_FALLBACK)
    assert ROLLUP_FALLBACK == "merged state alone, because GitHub refused the check rollup"


@pytest.mark.parametrize(
    ("state", "errors"),
    [
        ("OPEN", [{"type": "FORBIDDEN", "path": ROLLUP_PATH}]),
        ("MERGED", [{"type": "INTERNAL", "path": ROLLUP_PATH}]),
        ("MERGED", [{"type": "FORBIDDEN", "path": ["resource", "body"]}]),
        ("MERGED", [{"type": "FORBIDDEN", "path": [*ROLLUP_PATH[:-1], "file"]}]),
        ("MERGED", [{"type": "FORBIDDEN", "path": ROLLUP_PATH}, {"type": "INTERNAL", "path": ROLLUP_PATH}]),
        ("MERGED", [{"type": "FORBIDDEN"}]),
        ("MERGED", []),
    ],
)
def test_any_other_refused_read_stays_unknown(state, errors):
    assert single_view("https://github.com/o/r/pull/9", _refused(state, errors)) is None


def test_a_refused_read_with_unparsable_output_stays_unknown():
    run = lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="gh: forbidden", stderr="")  # noqa: E731
    assert single_view("https://github.com/o/r/pull/9", run) is None


def merged_with_empty_check_entries():
    raw = resource(state="MERGED")
    raw["mergedAt"] = "2026-10-21T10:00:00Z"
    commit = raw["commits"]["nodes"][0]["commit"]
    commit["statusCheckRollup"] = {
        "contexts": {"nodes": [None, {"name": "Tests", "conclusion": "SUCCESS"}], "pageInfo": {}}
    }
    commit["checkSuites"] = {"nodes": [None], "pageInfo": {}}
    stdout = json.dumps({"data": {"resource": raw}})
    return lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def test_a_merged_pull_request_with_empty_check_entries_reads_merged():
    pull = single_view("https://github.com/o/r/pull/9", merged_with_empty_check_entries())
    assert (pull.state, pull.merged_at, pull.head, pull.red, pull.checks_passed) == (
        "MERGED",
        1_792_576_800_000,
        "h1",
        False,
        True,
    )


def answering(answers: dict[str, dict[str, Any]], queries: list[str]) -> Callable[..., SimpleNamespace]:
    """Answer each batched read with the pull requests of ``answers`` its query names, in query order, and log the
    query."""

    def run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        query = command[-1]
        queries.append(query)
        named = [url for url in answers if json.dumps(url) in query]
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"data": {f"p{i}": answers[url] for i, url in enumerate(named)}})
        )

    return run


def landed(head: str) -> dict[str, Any]:
    """Return a pull request GitHub answers merged in full at ``head``."""
    return {**resource(head, "MERGED"), "mergedAt": "2026-10-21T10:00:00Z"}


@pytest.mark.feature("tick-view-merged-pull-cache")
def test_a_new_process_reads_only_the_pull_requests_not_kept_merged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import fakeredis

    opened, merged = "https://github.com/o/r/pull/1", "https://github.com/o/r/pull/2"
    cache = fakeredis.FakeRedis(decode_responses=True)
    queries: list[str] = []
    run = answering({opened: resource("h1"), merged: landed("h2")}, queries)
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "first"))
    first = views([opened, merged], run, cache)
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "second"))
    second = views([opened, merged], run, cache)

    assert len(queries) == 2
    assert json.dumps(opened) in queries[1]
    assert json.dumps(merged) not in queries[1]
    assert second == first
    assert second[merged].merged_at == 1_792_576_800_000
    assert cache.ttl(f"{PREFIX}:merged-pull-view:{merged}") == 2 * 24 * 3600


@pytest.mark.feature("tick-view-merged-pull-cache")
def test_open_and_closed_pull_requests_are_read_again_by_a_new_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import fakeredis

    opened, closed = "https://github.com/o/r/pull/1", "https://github.com/o/r/pull/3"
    cache = fakeredis.FakeRedis(decode_responses=True)
    queries: list[str] = []
    run = answering({opened: resource("h1"), closed: resource("h3", "CLOSED")}, queries)
    for home in ("first", "second"):
        monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / home))
        views([opened, closed], run, cache)

    assert [[json.dumps(url) in query for url in (opened, closed)] for query in queries] == [[True, True]] * 2
    assert cache.keys(f"{PREFIX}:merged-pull-view:*") == []


@pytest.mark.feature("tick-view-merged-pull-cache")
def test_a_kept_entry_with_other_fields_is_read_again_and_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import fakeredis

    merged = "https://github.com/o/r/pull/2"
    key = f"{PREFIX}:merged-pull-view:{merged}"
    cache = fakeredis.FakeRedis(decode_responses=True)
    cache.set(key, json.dumps({"state": "MERGED", "retired": True}))
    queries: list[str] = []
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))

    found = views([merged], answering({merged: landed("h2")}, queries), cache)

    assert len(queries) == 1
    assert found[merged].head == "h2"
    assert json.loads(str(cache.get(key)))["head"] == "h2"


def pull_view_lines(err: str) -> list[dict[str, Any]]:
    """Return the pull view count lines a view wrote to the controller log ``err``."""
    return [json.loads(line) for line in err.splitlines() if '"swarm_pull_views"' in line]


@pytest.mark.feature("tick-view-read-counts")
def test_a_fresh_tick_view_logs_that_redis_kept_the_merged_pull_request_and_github_read_the_open_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import time

    import fakeredis

    from scripts.inbox.store import InboxStore
    from scripts.swarm import ledger_events, metrics_outbox, stack, timing
    from scripts.swarm.store import RedisStore
    from tests.swarm.graph_port import RunPort, read_views

    opened, merged = "https://github.com/o/r/pull/1", "https://github.com/o/r/pull/2"
    doc = {
        "tasks": [{"id": "active", "state": "pr", "pr_url": opened}, {"id": "done", "state": "done", "pr_url": merged}],
        "_meta": {"events": [{"kind": "task done", "target": "tasks/done", "at": int(time.time() * 1000)}]},
    }
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    queries: list[str] = []
    run = answering({opened: resource("h1"), merged: landed("h2")}, queries)
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.setattr(metrics_outbox, "spool_path", lambda: tmp_path / "fresh-pod-history.sqlite")
    monkeypatch.setattr(stack, "login_source_control", lambda: RunPort(run))
    monkeypatch.setattr(ledger_events, "views", read_views)
    views([merged], run, store.redis)
    queries.clear()
    with timing.tick("sw"):
        token = timing.PASS.set({"pass_id": "pass-1"})
        first = ledger_events.tick_view(InboxStore(store.redis), store, "sw", doc)
        timing.PASS.reset(token)
        ledger_events.tick_view(InboxStore(store.redis), store, "sw", doc, first)
        ledger_events.tick_view(InboxStore(store.redis), store, "sw", doc)
    ledger_events.tick_view(InboxStore(store.redis), store, "sw", doc)

    assert queries and all(json.dumps(opened) in query and json.dumps(merged) not in query for query in queries)
    assert pull_view_lines(capsys.readouterr().err) == [
        {"event": "swarm_pull_views", "slug": "sw", "pass_id": "pass-1", "pulls": 2, "kept_merged": 1, "read": 1},
        {"event": "swarm_pull_views", "slug": "sw", "pass_id": None, "pulls": 2, "kept_merged": 1, "read": 1},
    ]


@pytest.mark.feature("tick-view-read-counts")
def test_a_pull_request_read_that_is_no_tick_view_logs_no_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import fakeredis

    from scripts.swarm import timing

    opened, merged = "https://github.com/o/r/pull/1", "https://github.com/o/r/pull/2"
    cache = fakeredis.FakeRedis(decode_responses=True)
    run = answering({opened: resource("h1"), merged: landed("h2")}, [])
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    views([opened, merged], run, cache)
    with timing.tick("sw"):
        views([opened, merged], run, cache)

    assert pull_view_lines(capsys.readouterr().err) == []


@pytest.mark.feature("tick-view-read-counts")
def test_a_tick_view_read_that_raises_leaves_later_reads_uncounted(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.swarm import ledger_events

    def refused(urls: list[str], cache: object = None, ttl: float = 0.0) -> dict[str, Any]:
        assert ledger_events.TICK_VIEW_READ.get() is True
        raise OSError("read refused")

    monkeypatch.setattr(ledger_events, "views", refused)

    with pytest.raises(OSError, match="read refused"):
        ledger_events._counted_views(["https://github.com/o/r/pull/1"], None, 180)
    assert ledger_events.TICK_VIEW_READ.get() is False


def test_an_empty_read_never_asks_the_shared_cache_for_no_keys() -> None:
    refusing = SimpleNamespace(mget=lambda keys: pytest.fail("an empty read asked the shared cache for no keys"))

    assert views([], lambda *a, **k: pytest.fail("empty request"), refusing) == {}


def batch_reply(command: list[str], **kwargs: Any) -> SimpleNamespace:
    """Answer a batch document with an open pull request for each of its resources."""
    count = command[-1].count("resource(url:")
    return SimpleNamespace(
        returncode=0, stdout=json.dumps({"data": {f"p{i}": resource(f"h{i}") for i in range(count)}})
    )


@pytest.mark.feature("tick-view-side-by-side-batches")
def test_a_views_call_reads_its_batch_documents_at_the_same_time() -> None:
    urls = [f"https://github.com/o/r/pull/{n}" for n in range(45)]
    meeting = threading.Barrier(3, timeout=5)
    queries: list[str] = []

    def run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        queries.append(command[-1])
        meeting.wait()
        return batch_reply(command)

    found = views(urls, run)

    assert sorted(query.count("resource(url:") for query in queries) == [5, 20, 20]
    assert all(found[url] is not None for url in urls)
    assert found[urls[44]].head == "h4"


def gated_pull(head: str, tree: str) -> dict[str, Any]:
    """Return an open pull request whose head commit holds workflow tree ``tree`` and passed the gate."""
    pr = resource(head)
    commit = pr["commits"]["nodes"][0]["commit"]
    commit["file"] = {"object": {"id": tree}}
    commit["statusCheckRollup"] = {"contexts": {"nodes": [{"name": GATE, "conclusion": "SUCCESS"}], "pageInfo": {}}}
    return pr


@pytest.mark.feature("tick-view-side-by-side-batches")
@pytest.mark.parametrize(
    ("shared", "cached", "tree_reads"), [(True, True, 1), (True, False, 2), (False, True, 2), (False, False, 2)]
)
def test_batches_make_the_sequential_github_calls_for_their_workflow_trees(
    shared: bool, cached: bool, tree_reads: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import fakeredis

    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    urls = [f"https://github.com/o/r/pull/{n}" for n in range(25)]
    cache = fakeredis.FakeRedis(decode_responses=True) if cached else None
    queries: list[str] = []

    def run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        query = command[-1]
        queries.append(query)
        if "nodes(ids:" in query:
            trees = [tree for tree in ("tree1", "tree2") if f'"{tree}"' in query]
            answer: dict[str, Any] = {
                "nodes": [
                    {"id": tree, "entries": [{"object": {"text": f"  gate:\n    name: {GATE}"}}]} for tree in trees
                ]
            }
        else:
            tree = "tree1" if shared or json.dumps(urls[0]) in query else "tree2"
            answer = {f"p{i}": gated_pull(f"h{i}", tree) for i in range(query.count("resource(url:"))}
        return SimpleNamespace(returncode=0, stdout=json.dumps({"data": answer}))

    found = views(urls, run, cache)

    assert len(queries) == 2 + tree_reads
    assert sum("nodes(ids:" in query for query in queries) == tree_reads
    assert all(found[url] is not None and found[url].gate_passed for url in urls)


@pytest.mark.feature("tick-view-side-by-side-batches")
def test_a_failed_batch_read_leaves_the_other_batch_read_once() -> None:
    urls = [f"https://github.com/o/r/pull/{n}" for n in range(25)]
    queries: list[str] = []

    def run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        queries.append(command[-1])
        if json.dumps(urls[0]) in command[-1]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return batch_reply(command)

    found = views(urls, run)

    assert len(queries) == 2
    assert [found[url] for url in urls[:20]] == [None] * 20
    assert all(found[url] is not None for url in urls[20:])


READER: contextvars.ContextVar[str] = contextvars.ContextVar("reader", default="none")


@pytest.mark.feature("tick-view-side-by-side-batches")
def test_each_views_batch_reads_in_the_callers_context() -> None:
    urls = [f"https://github.com/o/r/pull/{n}" for n in range(25)]
    readers: list[str] = []

    def run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        readers.append(READER.get())
        return batch_reply(command)

    token = READER.set("tick")
    try:
        views(urls, run)
    finally:
        READER.reset(token)

    assert readers == ["tick", "tick"]
