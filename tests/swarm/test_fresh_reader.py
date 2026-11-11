import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scripts.swarm import github_reads, ledger_events, mutation_wait
from scripts.swarm.ledger_events import PullRequest
from scripts.tick.ports.contracts import SourceControl
from tests.swarm.graph_port import RunPort

pytestmark = pytest.mark.unit

URLS = [f"https://github.com/o/r/pull/{n}" for n in range(3)]
MERGED = "https://github.com/o/r/pull/9"


def resource(head: str) -> dict[str, Any]:
    commit = {"committedDate": "2026-10-08T21:00:00Z", "file": None, "checkSuites": {"nodes": [], "pageInfo": {}}}
    return {
        "state": "OPEN",
        "mergedAt": None,
        "headRefOid": head,
        "mergeQueueEntry": None,
        "commits": {"nodes": [{"commit": {**commit, "statusCheckRollup": None}}]},
    }


def earlier(snapshots: dict[str, PullRequest | None]) -> Any:
    def lookup(url: str) -> PullRequest | None:
        return snapshots.get(url)

    lookup.snapshots = snapshots  # pyright: ignore[reportFunctionMemberAccess]
    return lookup


def held(*urls: str) -> Any:
    snapshots: dict[str, PullRequest | None] = {url: PullRequest("OPEN", None, None, False) for url in urls}
    snapshots[MERGED] = PullRequest("MERGED", 1, None, False)
    snapshots["https://github.com/o/r/pull/8"] = None
    return earlier(snapshots)


def head(pull: PullRequest | None) -> str:
    assert pull is not None
    return pull.head


@pytest.fixture
def port(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(mutation_wait, "backing_off", lambda: False)
    calls: list[str] = []
    missing: set[int] = set()

    def run(command: list[str], **kwargs: Any) -> SimpleNamespace:
        query = next(word for word in command if word.startswith("query="))
        calls.append(query)
        if "p0:resource" in query:
            data = {f"p{i}": None if i in missing else resource(f"fresh{i}") for i in range(query.count("resource("))}
        else:
            data = {"resource": resource("single")}
        return SimpleNamespace(returncode=0, stdout=json.dumps({"data": data}))

    return SimpleNamespace(source=cast(SourceControl, RunPort(run)), calls=calls, missing=missing)


def test_a_pass_rereads_every_open_pull_request_in_one_batched_request(port: Any) -> None:
    reread = ledger_events.fresh_reader(held(*URLS), port.source)
    assert head(reread(URLS[1])) == "fresh1"
    assert (head(reread(URLS[0])), head(reread(URLS[2])), head(reread(URLS[1]))) == ("fresh0", "fresh2", "fresh1")
    assert len(port.calls) == 1
    assert port.calls[0].count("resource(url:") == 3
    assert all(json.dumps(url) in port.calls[0] for url in URLS)
    assert MERGED not in port.calls[0]


def test_a_pull_request_the_batch_did_not_return_is_read_on_its_own(port: Any) -> None:
    port.missing.add(1)
    reread = ledger_events.fresh_reader(held(*URLS), port.source)
    assert head(reread(URLS[0])) == "fresh0"
    assert head(reread(URLS[1])) == "single"
    assert head(reread(MERGED)) == "single"
    assert len(port.calls) == 3
    assert [query.startswith("query=query($url:URI!)") for query in port.calls] == [False, True, True]


def test_a_view_holding_no_open_pull_request_reads_each_reread_on_its_own(port: Any) -> None:
    for lookup in (held(), {}.get):
        reread = ledger_events.fresh_reader(lookup, port.source)
        assert head(reread(URLS[0])) == "single"
    assert len(port.calls) == 2
    assert all("p0:resource" not in query for query in port.calls)


def test_a_reread_during_a_rate_limit_backoff_makes_no_request(port: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mutation_wait, "backing_off", lambda: True)
    reread = ledger_events.fresh_reader(held(*URLS), port.source)
    assert reread(URLS[0]) is None
    assert port.calls == []


def test_each_pass_reads_fresh_past_the_shared_snapshot(
    port: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    monkeypatch.setattr(github_reads, "home", lambda: tmp_path / "reads")
    cache = SimpleNamespace()
    for _ in range(2):
        assert head(ledger_events.fresh_reader(held(*URLS), port.source, cache)(URLS[0])) == "fresh0"
    assert len(port.calls) == 2


def test_the_batch_forwards_its_source_cache_and_a_zero_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mutation_wait, "backing_off", lambda: False)
    seen: list[tuple[Any, ...]] = []
    pull = PullRequest("OPEN", None, None, False, head="batched")

    def views(urls: list[str], source: Any = None, cache: Any = None, ttl: float = 0.5) -> dict[str, Any]:
        seen.append((urls, source, cache, ttl))
        return dict.fromkeys(urls, pull)

    monkeypatch.setattr(ledger_events, "views", views)
    reread = ledger_events.fresh_reader(held(URLS[0], URLS[1]), "source", "cache")  # pyright: ignore[reportArgumentType]
    assert reread(URLS[1]) is pull
    assert seen == [([URLS[0], URLS[1]], "source", "cache", 0)]
