import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scripts.swarm import github_reads, ledger_events, run_wait, stack
from scripts.swarm.ledger_events import PullRequest
from scripts.tick.ports.contracts import SourceControl
from tests.swarm.graph_port import RunPort

pytestmark = pytest.mark.unit

URLS = [f"https://github.com/o/r/pull/{n}" for n in range(3)]
MERGED = "https://github.com/o/r/pull/9"
REAL = (ledger_events.view, ledger_events.views)


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
    monkeypatch.setattr(run_wait, "backing_off", lambda: False)
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

    source = cast(SourceControl, RunPort(run))
    monkeypatch.setattr(stack, "login_source_control", lambda: source)
    monkeypatch.setattr(ledger_events, "view", REAL[0])
    monkeypatch.setattr(ledger_events, "views", REAL[1])
    return SimpleNamespace(calls=calls, missing=missing)


@pytest.mark.feature("ledger-events-fresh-reader-batches-rereads")
def test_a_pass_rereads_every_open_pull_request_in_one_batched_request(port: Any) -> None:
    reread = ledger_events.fresh_reader(held(*URLS))
    assert head(reread(URLS[1])) == "fresh1"
    assert (head(reread(URLS[0])), head(reread(URLS[2])), head(reread(URLS[1]))) == ("fresh0", "fresh2", "fresh1")
    assert len(port.calls) == 1
    assert port.calls[0].count("resource(url:") == 3
    assert all(json.dumps(url) in port.calls[0] for url in URLS)
    assert MERGED not in port.calls[0]


@pytest.mark.feature("ledger-events-fresh-reader-batches-rereads")
def test_a_pull_request_the_batch_did_not_return_is_read_on_its_own(port: Any) -> None:
    port.missing.add(1)
    reread = ledger_events.fresh_reader(held(*URLS))
    assert head(reread(URLS[0])) == "fresh0"
    assert head(reread(URLS[1])) == "single"
    assert head(reread(MERGED)) == "single"
    assert len(port.calls) == 3
    assert [query.startswith("query=query($url:URI!)") for query in port.calls] == [False, True, True]


@pytest.mark.feature("ledger-events-fresh-reader-batches-rereads")
def test_a_view_holding_no_open_pull_request_reads_each_reread_on_its_own(port: Any) -> None:
    for lookup in (held(), {}.get):
        reread = ledger_events.fresh_reader(lookup)
        assert head(reread(URLS[0])) == "single"
    assert len(port.calls) == 2
    assert all("p0:resource" not in query for query in port.calls)


@pytest.mark.feature("ledger-events-fresh-reader-batches-rereads")
def test_a_reread_during_a_rate_limit_backoff_makes_no_request(port: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_wait, "backing_off", lambda: True)
    reread = ledger_events.fresh_reader(held(*URLS))
    assert reread(URLS[0]) is None
    assert port.calls == []


@pytest.mark.feature("ledger-events-fresh-reader-batches-rereads")
def test_each_pass_reads_fresh_past_the_shared_snapshot(
    port: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    import fakeredis

    monkeypatch.setattr(github_reads, "home", lambda: tmp_path / "reads")
    cache = fakeredis.FakeRedis(decode_responses=True)
    for _ in range(2):
        assert head(ledger_events.fresh_reader(held(*URLS), cache)(URLS[0])) == "fresh0"
    assert len(port.calls) == 2


@pytest.mark.feature("ledger-events-fresh-reader-batches-rereads")
def test_the_batch_forwards_its_cache_and_a_zero_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_wait, "backing_off", lambda: False)
    seen: list[tuple[Any, ...]] = []
    pull = PullRequest("OPEN", None, None, False, head="batched")

    def views(urls: list[str], cache: Any = None, ttl: float = 0.5) -> dict[str, Any]:
        seen.append((urls, cache, ttl))
        return dict.fromkeys(urls, pull)

    monkeypatch.setattr(ledger_events, "views", views)
    reread = ledger_events.fresh_reader(held(URLS[0], URLS[1]), "cache")
    assert reread(URLS[1]) is pull
    assert seen == [([URLS[0], URLS[1]], "cache", 0)]


@pytest.mark.feature("swarm-waits-end-journals-its-phases")
def test_the_batched_reread_is_journaled_as_one_views_row(port: Any, capsys: pytest.CaptureFixture[str]) -> None:
    from scripts.swarm import timing

    reread = ledger_events.fresh_reader(held(*URLS))
    with timing.tick("sw"):
        assert head(reread(URLS[0])) == "fresh0"
        assert head(reread(URLS[1])) == "fresh1"
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    journaled = [
        line["step"]
        for line in lines
        if line.get("event") == "swarm_tick_step"
        and line.get("phase") == "finished"
        and line["step"].startswith("scripts.swarm.ledger_events.")
    ]
    assert journaled == ["scripts.swarm.ledger_events.views"]
