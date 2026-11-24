import inspect
from collections.abc import Iterator
from typing import Any

import pytest

from scripts.swarm_ledger import ledger_server as server
from scripts.swarm_ledger import watch_ledger
from tests.swarm_ledger import test_watch_ledger as replay_tests

live = replay_tests.live


def beat_once_per_sequence(monkeypatch):
    wait, beaten = server.HUB.wait, set()

    def beat_first(slug, seq, timeout):
        if seq in beaten:
            return wait(slug, seq, timeout)
        beaten.add(seq)
        return []

    monkeypatch.setattr(server.HUB, "wait", beat_first)


@pytest.mark.parametrize("server_beat", [False, True])
@pytest.mark.parametrize("heartbeats", [1, 3])
def test_live_replay_finishes_with_forced_heartbeats(
    live: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    heartbeats: int,
    server_beat: bool,
) -> None:
    if server_beat:
        beat_once_per_sequence(monkeypatch)
    original = watch_ledger.stream
    start = replay_tests.repository.get_document(replay_tests.SLUG)["_meta"]["rev"]
    frames = []
    received = []

    def forced(
        slug: str, cursor: str | None = None, headers: dict[str, str] | None = None
    ) -> Iterator[tuple[str, Any, str | None]]:
        if frames:
            assert inspect.getgeneratorstate(frames[-1]) == inspect.GEN_CLOSED
        source = original(slug, cursor, headers)
        frames.append(source)
        try:
            if cursor:
                for _ in range(heartbeats):
                    yield "heartbeat", {}, None
            for name, data, event_id in source:
                received.append((cursor, name, data.get("rev")))
                yield name, data, event_id
                if name == "open" and data.get("rev") is None:
                    for _ in range(heartbeats):
                        yield "heartbeat", {}, None
                elif cursor is None and (data.get("rev") or 0) > start:
                    pytest.fail("watcher did not reconnect after the first ledger change with forced heartbeats")
        finally:
            source.close()

    streams = []

    def retained(slug, cursor=None, headers=None):
        result = forced(slug, cursor, headers)
        streams.append(result)
        return result

    monkeypatch.setattr(watch_ledger, "stream", retained)
    try:
        replay_tests.test_the_watcher_prints_live_changes_and_replays_what_it_missed_after_a_drop(
            live, monkeypatch, capsys
        )
        assert received[0][1:] == ("open", None)
        assert [(bool(cursor), rev) for cursor, _, rev in received if (rev or 0) > start] == [
            (False, start + 1),
            (True, start + 1),
            (True, start + 2),
        ]
        assert received[-1][0]
        assert all(inspect.getgeneratorstate(item) == inspect.GEN_CLOSED for item in streams + frames)
    finally:
        for item in streams + frames:
            item.close()
