"""A ledger watch waits out a dropped shared ledger database and resumes from its cursor without printing an event
twice, and stops with its reason on a refusal that a reconnect with a fresh credential cannot clear."""

import io
import sqlite3
import sys
import types
import urllib.error
from collections.abc import Callable, Iterator
from email.message import Message
from typing import Any

import psycopg2
import pytest

from scripts.swarm_ledger import storage, watch_ledger
from scripts.swarm_ledger.repository import postgres
from scripts.tick.adapters import database_ledger

SLUG = "watchreconnect-2026-01-01"
DROPPED = "SSL connection has been closed unexpectedly"


def dropped(message: str = DROPPED) -> sqlite3.DatabaseError:
    """The error the ledger database layer raises when its PostgreSQL connection drops mid read."""
    with pytest.raises(sqlite3.DatabaseError) as raised, postgres.mapped():
        raise psycopg2.OperationalError(message)
    return raised.value


def refusal(code: int, body: bytes, message: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        f"http://ledger/api/v1/ledgers/{SLUG}/events", code, message, Message(), io.BytesIO(body)
    )


def ledger(rev: int, *events: dict[str, Any]) -> dict[str, Any]:
    return {"tasks": [], "_meta": {"rev": rev, "events": list(events), "members": {}}}


def event(rev: int) -> dict[str, Any]:
    return {"rev": rev, "by": "operator", "kind": "note added", "id": f"n-{rev}", "text": f"note {rev}"}


def snapshot(rev: int, cursor: str | None) -> tuple[str, dict[str, Any], str | None]:
    return "snapshot", {"ledger": ledger(rev, *(event(n) for n in range(4, rev + 1)))}, cursor


def watched(monkeypatch: pytest.MonkeyPatch, *flags: str) -> list[float]:
    """Point main at a ledger that exists, with no credential to fetch; the waits main sleeps, in order."""
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        assert len(sleeps) <= 20, "the watch kept retrying"

    monkeypatch.setattr(storage, "default", lambda: types.SimpleNamespace(exists=lambda slug: True))
    monkeypatch.setattr(watch_ledger, "credentials", lambda slug: {})
    monkeypatch.setattr(watch_ledger, "time", types.SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(sys, "argv", ["watch_ledger.py", SLUG, "--since-rev", "3", *flags])
    return sleeps


def scripted(outcomes: list[Any], cursors: list[str | None]) -> Callable[..., Iterator[Any]]:
    """A stream that, per connect, raises the next exception or yields the next list of frames, recording its cursor."""
    pending = iter(outcomes)

    def stream(slug: str, cursor: str | None = None, headers: dict[str, str] | None = None) -> Iterator[Any]:
        cursors.append(cursor)
        outcome = next(pending)
        if isinstance(outcome, BaseException):
            raise outcome
        yield from outcome

    return stream


def operator_lines(out: str) -> list[str]:
    return [text for text in out.splitlines() if text.startswith("OPERATOR")]


@pytest.mark.feature("ledger-watch-database-reconnect")
def test_a_database_drop_mid_stream_resumes_from_the_cursor_and_prints_each_event_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sleeps = watched(monkeypatch, "--interval", "0.5")
    cursors: list[str | None] = []

    def first() -> Iterator[Any]:
        yield snapshot(5, "c-5")
        raise dropped()

    outcomes = [first(), [snapshot(6, "c-6")], SystemExit()]
    monkeypatch.setattr(watch_ledger, "stream", scripted(outcomes, cursors))
    with pytest.raises(SystemExit):
        watch_ledger.main()
    out = capsys.readouterr().out
    assert operator_lines(out) == [
        'OPERATOR rev=4 note added [n-4]: "note 4"',
        'OPERATOR rev=5 note added [n-5]: "note 5"',
        'OPERATOR rev=6 note added [n-6]: "note 6"',
    ]
    assert f"WARNING ledger stream: {DROPPED}" in out.splitlines()
    assert cursors == [None, "c-5", "c-6"]
    assert sleeps == [0.5, 0.5]


@pytest.mark.feature("ledger-watch-database-reconnect")
def test_a_database_drop_on_the_remote_read_keeps_the_watch_and_its_kept_exports(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sleeps = watched(monkeypatch, "--interval", "7")
    answers = iter([ledger(5, event(4), event(5)), dropped("connection already closed"), ledger(6, event(5), event(6))])
    kept: list[Any] = []

    class Client:
        def export(self, slug: str, exports: dict[Any, Any]) -> dict[str, Any]:
            kept.append(exports)
            answer = next(answers, None)
            if answer is None:
                raise SystemExit
            if isinstance(answer, BaseException):
                raise answer
            return answer

    monkeypatch.setattr(watch_ledger, "remote", lambda slug: True)
    monkeypatch.setattr(database_ledger, "client", lambda base, timeout: Client())
    with pytest.raises(SystemExit):
        watch_ledger.main()
    out = capsys.readouterr().out
    assert operator_lines(out) == [
        'OPERATOR rev=4 note added [n-4]: "note 4"',
        'OPERATOR rev=5 note added [n-5]: "note 5"',
        'OPERATOR rev=6 note added [n-6]: "note 6"',
    ]
    assert out.count("WATCHING") == 1
    assert "WARNING ledger stream: connection already closed" in out.splitlines()
    assert len(kept) == 4 and all(exports is watch_ledger.EXPORTS for exports in kept)
    assert sleeps == [7.0, 7.0, 7.0]


@pytest.mark.feature("ledger-watch-database-reconnect")
@pytest.mark.parametrize(
    ("interval", "waits"),
    [
        ("2", [2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 2.0, 2.0, 4.0]),
        ("40", [40.0, 40.0, 40.0, 40.0, 40.0, 40.0, 40.0, 40.0, 40.0]),
    ],
)
def test_repeated_database_drops_wait_longer_up_to_a_cap_and_start_over_after_a_frame(
    interval: str, waits: list[float], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sleeps = watched(monkeypatch, "--interval", interval)
    cursors: list[str | None] = []
    outcomes = [*(dropped() for _ in range(6)), [("heartbeat", {}, None)], dropped(), dropped(), SystemExit()]
    monkeypatch.setattr(watch_ledger, "stream", scripted(outcomes, cursors))
    with pytest.raises(SystemExit):
        watch_ledger.main()
    assert sleeps == waits
    assert capsys.readouterr().out.splitlines() == [f"WARNING ledger stream: {DROPPED}"] * 2


@pytest.mark.feature("ledger-watch-database-reconnect")
@pytest.mark.parametrize(
    ("code", "body", "message", "reason"),
    [
        (404, b'{"error": {"code": "ledger_missing", "message": "No such ledger"}}', "Not Found", "No such ledger"),
        (
            403,
            b'{"error": {"code": "forbidden", "message": "Missing or wrong ledger credential"}}',
            "Forbidden",
            "Missing or wrong ledger credential",
        ),
        (401, b"<html>sign in</html>", "Unauthorized", "Unauthorized"),
        (401, b'{"error": "denied"}', "Unauthorized", "Unauthorized"),
    ],
)
def test_a_refusal_that_repeats_on_a_fresh_credential_stops_the_watch_with_its_reason(
    code: int,
    body: bytes,
    message: str,
    reason: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sleeps = watched(monkeypatch, "--interval", "0.5")
    reads: list[str] = []

    def credentials(slug: str) -> dict[str, str]:
        reads.append(slug)
        return {"X-Ledger-Token": f"t{len(reads)}"}

    def refusing(slug: str, cursor: str | None = None, headers: dict[str, str] | None = None) -> Iterator[Any]:
        raise refusal(code, body, message)
        yield

    monkeypatch.setattr(watch_ledger, "credentials", credentials)
    monkeypatch.setattr(watch_ledger, "stream", refusing)
    with pytest.raises(SystemExit) as stopped:
        watch_ledger.main()
    assert stopped.value.code == 1
    assert capsys.readouterr().out.splitlines() == [
        f"WARNING ledger stream: HTTP Error {code}: {message}",
        f"STOPPED ledger watch: HTTP {code} {reason}",
    ]
    assert reads == [SLUG, SLUG]
    assert sleeps == [0.5]


@pytest.mark.feature("ledger-watch-database-reconnect")
def test_a_refusal_cleared_by_a_fresh_credential_keeps_the_watch_streaming(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sleeps = watched(monkeypatch, "--interval", "0.5")
    forbidden = b'{"error": {"code": "forbidden", "message": "Missing or wrong ledger credential"}}'
    reads: list[str] = []
    cursors: list[str | None] = []

    def credentials(slug: str) -> dict[str, str]:
        reads.append(slug)
        return {"X-Ledger-Token": f"t{len(reads)}"}

    outcomes = [
        refusal(403, forbidden, "Forbidden"),
        [snapshot(5, "c-5")],
        refusal(403, forbidden, "Forbidden"),
        [snapshot(6, "c-6")],
        SystemExit(),
    ]
    monkeypatch.setattr(watch_ledger, "credentials", credentials)
    monkeypatch.setattr(watch_ledger, "stream", scripted(outcomes, cursors))
    with pytest.raises(SystemExit):
        watch_ledger.main()
    out = capsys.readouterr().out
    assert "STOPPED" not in out
    assert operator_lines(out)[-1] == 'OPERATOR rev=6 note added [n-6]: "note 6"'
    assert len(reads) == 3
    assert cursors == [None, None, "c-5", "c-5", "c-6"]
    assert sleeps == [0.5] * 4


@pytest.mark.feature("ledger-watch-database-reconnect")
@pytest.mark.parametrize(
    ("code", "body", "message"),
    [
        (404, b"<html>404 page not found</html>", "Not Found"),
        (404, b'{"detail": "no route"}', "Not Found"),
        (404, b'{"error": {"code": "resource_missing", "message": "No such resource"}}', "Not Found"),
        (403, b"[]", "Forbidden"),
    ],
)
def test_a_403_or_404_without_the_ledger_refusal_code_keeps_retrying_at_the_interval(
    code: int, body: bytes, message: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sleeps = watched(monkeypatch, "--interval", "0.5")
    cursors: list[str | None] = []
    outcomes = [refusal(code, body, message), refusal(code, body, message), refusal(code, body, message), SystemExit()]
    monkeypatch.setattr(watch_ledger, "stream", scripted(outcomes, cursors))
    with pytest.raises(SystemExit) as stopped:
        watch_ledger.main()
    assert stopped.value.code is None
    assert capsys.readouterr().out.splitlines() == [f"WARNING ledger stream: HTTP Error {code}: {message}"]
    assert sleeps == [0.5] * 3


@pytest.mark.feature("ledger-watch-database-reconnect")
def test_a_server_error_or_a_lost_connection_keeps_retrying_at_the_interval(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sleeps = watched(monkeypatch, "--interval", "0.5")
    cursors: list[str | None] = []
    outcomes = [
        refusal(503, b"", "Service Unavailable"),
        refusal(503, b"", "Service Unavailable"),
        urllib.error.URLError("connection refused"),
        refusal(502, b"", "Bad Gateway"),
        SystemExit(),
    ]
    monkeypatch.setattr(watch_ledger, "stream", scripted(outcomes, cursors))
    with pytest.raises(SystemExit) as stopped:
        watch_ledger.main()
    assert stopped.value.code is None
    assert "STOPPED" not in capsys.readouterr().out
    assert sleeps == [0.5] * 4


@pytest.mark.feature("ledger-watch-database-reconnect")
def test_the_startup_ledger_check_waits_out_a_database_drop(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sleeps = watched(monkeypatch, "--interval", "0.5")
    answers = iter([dropped(), dropped(), True])
    cursors: list[str | None] = []

    def exists(slug: str) -> bool:
        answer = next(answers)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(storage, "default", lambda: types.SimpleNamespace(exists=exists))
    monkeypatch.setattr(watch_ledger, "stream", scripted([SystemExit()], cursors))
    with pytest.raises(SystemExit):
        watch_ledger.main()
    assert capsys.readouterr().out.splitlines() == [f"WARNING ledger stream: {DROPPED}"]
    assert sleeps == [0.5, 1.0]
    assert cursors == [None]
