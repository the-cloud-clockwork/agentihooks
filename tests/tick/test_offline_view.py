"""A tick pass whose ledger server is slow or down runs from the last good view this host stored."""

import contextvars
import io
import urllib.error
from contextlib import ExitStack
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import cli, ledger_client
from scripts.swarm.ledger_client import OFFLINE, LedgerClient, LedgerUnreachable
from scripts.swarm_ledger.api import client as api
from scripts.tick import view
from scripts.tick.adapters.ledger import resource_client
from scripts.tick.steps import host as host_steps
from scripts.tick.steps import masters, observe, tasks

STATE = {
    "tasks": [{"id": "t1", "state": "open"}, {"id": "t2", "state": "claimed"}],
    "_meta": {"rev": 4, "events": []},
    "phases": [{"id": "p1", "done": False}],
}

Queued = list[tuple[str, list[dict[str, Any]], bool]]


@pytest.fixture
def stored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Queued:
    """A ledger module that queues every write and refuses calls, with the view of ``sw`` stored at generation 7."""
    from scripts.scheduler import service

    queued: list[tuple[str, list[dict[str, Any]], bool]] = []

    def queue(slug: str, ops: list[dict[str, Any]], service: bool) -> dict[str, Any]:
        queued.append((slug, ops, service))
        return {"queued": ops[0]["id"]}

    def call(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise AssertionError("an offline pass never calls the server")

    ledger = SimpleNamespace(
        base=lambda slug: "http://ledger" if slug == "sw" else "http://elsewhere",
        credentials=lambda slug, service: {},
        REQUEST_TIMEOUT=4,
        queue=queue,
        call=call,
        Missing=SystemExit,
    )
    monkeypatch.setattr(ledger_client, "_ledger", lambda: ledger)
    monkeypatch.setattr(service, "runtime_dir", lambda: tmp_path)
    api.write_export_file(view.view_file("http://ledger", "sw", tmp_path), 7, STATE)
    return queued


@pytest.mark.feature("swarm-tick-cached-ledger")
def test_an_offline_pass_holds_the_stored_view_and_queues_its_writes_onto_it(stored: Queued) -> None:
    with view.holding():
        held = view.hold_stored("sw")
        assert held is not None and held.generation == 7
        assert view.document("sw") == STATE
        token = OFFLINE.set(True)
        try:
            task = LedgerClient(service=True).update_task("sw", "t1", {"state": "claimed", "claimed_by": "e1"})
        finally:
            OFFLINE.reset(token)
        assert task == {"id": "t1", "state": "claimed", "claimed_by": "e1"}
        [(slug, ops, service)] = stored
        assert (slug, ops[0]["op"], ops[0]["item"], service) == ("sw", "task_update", "tasks/t1", True)
        view.wrote()
        current = view.current()
        assert current is not None and current.generation == 7
        assert current.read("tasks", "t1") == {"id": "t1", "state": "claimed", "claimed_by": "e1"}
        assert current.read("tasks", "t2") == {"id": "t2", "state": "claimed"}
        document = view.document("sw")
        assert document is not None and document["_meta"] == STATE["_meta"]


@pytest.mark.feature("swarm-tick-cached-ledger")
def test_an_offline_pass_refuses_fresh_reads_at_once(stored: Queued) -> None:
    client = LedgerClient()
    token = OFFLINE.set(True)
    try:
        for read in (client.state, client.metadata, client.tasks):
            with pytest.raises(LedgerUnreachable, match="last good view"):
                read("sw")
        with pytest.raises(LedgerUnreachable, match="last good view"):
            resource_client("sw")(5.0)
    finally:
        OFFLINE.reset(token)
    assert stored == []


@pytest.mark.feature("swarm-tick-cached-ledger")
def test_a_host_without_a_stored_view_holds_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.scheduler import service

    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(base=lambda slug: "http://ledger"))
    monkeypatch.setattr(service, "runtime_dir", lambda: tmp_path)
    with view.holding():
        assert view.hold_stored("sw") is None
        assert view.current() is None
        assert view.document("sw") is None


@pytest.mark.feature("swarm-tick-cached-ledger")
def test_queued_writes_without_a_held_view_patch_nothing() -> None:
    with view.holding():
        assert view.queued([{"op": "task_update", "item": "tasks/t1", "fields": {"state": "claimed"}}]) == {}


def refusal(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://ledger/export", code, "refused", Message(), io.BytesIO(b"{}"))


@pytest.mark.feature("swarm-tick-cached-ledger")
@pytest.mark.parametrize(
    "failure",
    [urllib.error.URLError("refused"), TimeoutError("timed out"), refusal(503)],
    ids=["stopped", "slow", "failing"],
)
def test_a_slow_or_down_server_leaves_the_pass_offline_on_the_stored_view(
    failure: OSError, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    timeouts: list[float | None] = []
    stored = view.LedgerView(7, STATE)

    def start(slug: str, service: bool = False, timeout: float | None = None) -> None:
        timeouts.append(timeout)
        raise failure

    monkeypatch.setattr(cli.ledger_view, "start", start)
    monkeypatch.setattr(cli.ledger_view, "hold_stored", lambda slug: stored if slug == "sw" else None)
    monkeypatch.setattr(cli.ledger_view, "fresh", lambda slug: True)
    with ExitStack() as beating:
        cli._hold_view(beating, "sw", LedgerClient(service=True))
        assert OFFLINE.get() is True
    assert OFFLINE.get() is False
    assert timeouts == [ledger_client.SNAPSHOT_TIMEOUT_S]
    assert "ledger sw not answering" in capsys.readouterr().err


@pytest.mark.feature("swarm-tick-cached-ledger")
def test_a_down_server_without_a_stored_view_leaves_the_pass_to_its_health_probe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def start(slug: str, service: bool = False, timeout: float | None = None) -> None:
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(cli.ledger_view, "start", start)
    monkeypatch.setattr(cli.ledger_view, "hold_stored", lambda slug: None)
    with ExitStack() as beating:
        cli._hold_view(beating, "sw", LedgerClient())
        assert OFFLINE.get() is False
        assert view.current() is None
    assert OFFLINE.get() is False
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ledger sw not answering (<urlopen error refused>) and no view is stored to run from" in captured.err


@pytest.mark.feature("swarm-tick-cached-ledger")
def test_a_refusing_server_still_ends_the_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    def start(slug: str, service: bool = False, timeout: float | None = None) -> None:
        raise refusal(401)

    monkeypatch.setattr(cli.ledger_view, "start", start)
    with ExitStack() as beating, pytest.raises(urllib.error.HTTPError):
        cli._hold_view(beating, "sw", LedgerClient())
    assert OFFLINE.get() is False


@pytest.mark.feature("swarm-tick-cached-ledger")
def test_a_cleartext_ledger_address_leaves_the_pass_to_its_health_probe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.swarm_ledger.ledger_link import Cleartext

    def start(slug: str, service: bool = False, timeout: float | None = None) -> None:
        raise Cleartext("a ledger credential off loopback travels only over https")

    monkeypatch.setattr(cli.ledger_view, "start", start)
    monkeypatch.setattr(cli.ledger_view, "hold_stored", lambda slug: pytest.fail("a cleartext address holds no view"))
    with ExitStack() as beating:
        cli._hold_view(beating, "sw", LedgerClient(service=True))
        assert OFFLINE.get() is False
        assert view.current() is None
    assert (
        "ledger sw view not held: a ledger credential off loopback travels only over https" in capsys.readouterr().err
    )


@pytest.mark.feature("swarm-tick-cached-ledger")
@pytest.mark.parametrize(
    "guard",
    [{"if_state": ["claimed"]}, {"if_match": {"claimed_by": "e2"}}],
    ids=["state", "match"],
)
def test_an_offline_write_whose_guard_fails_on_the_held_view_is_refused(stored: Queued, guard: dict[str, Any]) -> None:
    from scripts.swarm.ledger_client import LedgerRefused

    with view.holding():
        view.hold_stored("sw")
        token = OFFLINE.set(True)
        try:
            with pytest.raises(LedgerRefused, match="ledger sw refused: tasks/t1 moved since the last good view"):
                LedgerClient().update_task("sw", "t1", {"claimed_by": "e1"}, **guard)
        finally:
            OFFLINE.reset(token)
        current = view.current()
        assert current is not None and current.read("tasks", "t1") == {"id": "t1", "state": "open"}
    assert stored == []


@pytest.mark.feature("swarm-tick-cached-ledger")
def test_an_offline_write_whose_guard_holds_is_queued(stored: Queued) -> None:
    with view.holding():
        view.hold_stored("sw")
        token = OFFLINE.set(True)
        try:
            task = LedgerClient().update_task(
                "sw", "t1", {"claimed_by": "e1"}, if_state=("open",), if_match={"claimed_by": "", "claim_generation": 0}
            )
        finally:
            OFFLINE.reset(token)
    assert task == {"id": "t1", "state": "open", "claimed_by": "e1"}
    assert len(stored) == 1


@pytest.mark.feature("swarm-tick-cached-ledger")
def test_an_offline_write_matching_a_held_field_is_queued(stored: Queued) -> None:
    with view.holding():
        view.hold_stored("sw")
        token = OFFLINE.set(True)
        try:
            task = LedgerClient().update_task("sw", "t2", {"claimed_by": "e1"}, if_match={"claimed_by": ""})
        finally:
            OFFLINE.reset(token)
    assert task == {"id": "t2", "state": "claimed", "claimed_by": "e1"}
    assert len(stored) == 1


@pytest.mark.feature("swarm-tick-cached-ledger")
def test_an_offline_write_without_a_held_view_is_queued_unguarded(stored: Queued) -> None:
    with view.holding():
        token = OFFLINE.set(True)
        try:
            task = LedgerClient().update_task("sw", "t1", {"claimed_by": "e1"}, if_state=("claimed",))
        finally:
            OFFLINE.reset(token)
    assert task == {}
    assert len(stored) == 1


@pytest.mark.feature("swarm-tick-cached-ledger")
def test_a_first_read_stores_the_view_even_at_generation_zero(tmp_path: Path) -> None:
    path = tmp_path / "view.json"
    held = view.refreshed(path, lambda since, parts: {"generation": 0, "data": STATE})
    assert held.generation == 0
    assert api.read_export_file(path) == (0, STATE)


@pytest.mark.feature("swarm-tick-cached-ledger")
def test_a_server_that_stops_mid_pass_keeps_the_held_view_until_it_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.scheduler import service

    replies: list[dict[str, Any] | OSError] = [
        {"generation": 3, "data": STATE},
        TimeoutError("timed out"),
        refusal(401),
    ]

    def changes(
        self: api.ResourceClient, slug: str, since: int, parts: tuple[str, ...], scope: object = None
    ) -> dict[str, Any]:
        reply = replies.pop(0)
        if isinstance(reply, OSError):
            raise reply
        return reply

    ledger = SimpleNamespace(base=lambda slug: "http://ledger", credentials=lambda slug, service: {}, REQUEST_TIMEOUT=4)
    monkeypatch.setattr(ledger_client, "_ledger", lambda: ledger)
    monkeypatch.setattr(service, "runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(api.ResourceClient, "changes", changes)
    with view.holding(), ExitStack() as reset:
        reset.callback(OFFLINE.set, False)
        held = view.start("sw", False)
        assert OFFLINE.get() is False
        view.wrote()
        assert contextvars.copy_context().run(view.current) is held
        assert ledger_client.offline() is True
        view.wrote()
        with pytest.raises(urllib.error.HTTPError):
            view.current()


@pytest.mark.feature("swarm-tick-cached-ledger")
@pytest.mark.parametrize("kept", [True, False], ids=["stored", "none"])
def test_a_failed_health_probe_stops_the_pass_only_without_a_stored_view(
    kept: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm import outage
    from scripts.tick.steps import host

    monkeypatch.setattr(outage, "ledger_health", lambda slug, at, probe: False)
    monkeypatch.setattr(host.ledger_view, "stored", lambda slug: view.LedgerView(7, STATE) if kept else None)
    box: dict[str, Any] = {}
    found = {"ledger": LedgerClient(), "slug": "sw", "box": box}
    host._ledger_health(SimpleNamespace(now_ms=lambda: 1000), found.__getitem__)
    assert box == ({} if kept else {"stop": ["Ledger health probe failed"]})


def _refuse_fresh(*_args: object, **_kwargs: object) -> None:
    raise ledger_client.LedgerOffline(f"ledger sw: {ledger_client.OFFLINE_READ}")


def _read_events(slug: str) -> None:
    raise ledger_client.LedgerOffline(f"ledger {slug}: {ledger_client.OFFLINE_READ}")


def _observed(_tick: Any, get: Any) -> Any:
    return observe._run(_read_events, None, get)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.feature("swarm-tick-cached-ledger")
@pytest.mark.parametrize(
    "wrapped",
    [
        host_steps.held(_refuse_fresh),
        masters._held(_refuse_fresh),  # pyright: ignore[reportPrivateUsage]
        tasks.held(_refuse_fresh),
        _observed,
    ],
)
def test_a_step_needing_a_fresh_read_is_skipped_offline_and_the_pass_goes_on(wrapped: Any) -> None:
    box: dict[str, Any] = {}
    values = {"box": box, "slug": "sw", "store": None, "ledger": None, "runtime": None, "now_ms": 0}

    assert wrapped(None, values.__getitem__) == []
    assert "raised" not in box
