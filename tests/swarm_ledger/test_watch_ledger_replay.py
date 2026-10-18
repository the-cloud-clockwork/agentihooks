import inspect

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
def test_live_replay_finishes_with_forced_heartbeats(live, monkeypatch, capsys, heartbeats, server_beat):
    if server_beat:
        beat_once_per_sequence(monkeypatch)
    original = watch_ledger.stream
    frames = []
    received = []

    def forced(slug, cursor=None, headers=None):
        if frames:
            assert inspect.getgeneratorstate(frames[-1]) == inspect.GEN_CLOSED
        source = original(slug, cursor, headers)
        frames.append(source)
        try:
            if cursor:
                for _ in range(heartbeats):
                    yield "heartbeat", {}, None
            for name, data, event_id in source:
                received.append((cursor, name))
                yield name, data, event_id
                if name == "snapshot":
                    for _ in range(heartbeats):
                        yield "heartbeat", {}, None
                elif name == "ledger" and cursor is None:
                    pytest.fail("watcher did not reconnect after the first ledger event with forced heartbeats")
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
        assert [name for _, name in received if name != "heartbeat"] == ["snapshot", "ledger", "ledger", "ledger"]
        assert received[-1][0]
        assert all(inspect.getgeneratorstate(item) == inspect.GEN_CLOSED for item in streams + frames)
    finally:
        for item in streams + frames:
            item.close()
