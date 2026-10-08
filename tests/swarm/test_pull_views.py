import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts.swarm.ledger_events import GATE, SENT_TTL_S, views
from scripts.swarm.store import PREFIX


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
        count = 20 if len(calls) == 1 else 1
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"data": {f"p{i}": resource(f"h{i}") for i in range(count)}})
        )

    found = views(urls + urls, run)
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


def test_empty_batch_never_launches_a_command():
    assert views([], lambda *a, **k: pytest.fail("empty request")) == {}


def test_workflow_declarations_cache_by_immutable_tree_while_heads_and_checks_refresh():
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
                query == f'query={{nodes(ids:["{tree}"]){{id ...on Tree{{entries{{object{{...on Blob{{text}}}}}}}}}}}}'
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

    first = views([url], run, cache)[url]
    second = views([url], run, cache)[url]
    third = views([url], run, cache)[url]
    fourth = views([url], run, cache)[url]
    assert len(calls) == 6
    assert fourth.gate_passed is False
    assert first.head != second.head
    assert first.gate_passed and second.gate_passed
    assert not third.gate_passed
    assert "Blob{text}" not in calls[0]
    assert all(key.startswith(f"{PREFIX}:") for key in cache.scan_iter())
    assert 0 < cache.ttl(f"{PREFIX}:workflow-gate:tree1") <= SENT_TTL_S


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
    assert len(calls) == 4
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
    monkeypatch.setattr(ledger_events, "views", lambda urls, cache: batched.append(urls) or {url: None for url in urls})
    monkeypatch.setattr(ledger_events, "view", lambda url: fallback.append(url))
    lookup = ledger_events.tick_view(inbox, store, "sw", doc)
    assert batched == [["active"]] if doc else batched == [[]]
    if doc:
        assert lookup("active") is None
        assert fallback == []
    lookup("other")
    lookup("other")
    assert fallback == ["other"]


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
