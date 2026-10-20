from types import SimpleNamespace

import pytest

from scripts.doctor import traces
from scripts.swarm import cli, idle, waits


def record(held, start=60_000, end=12_540_000):
    return {
        "waits": {"engineer": held},
        "traces": [
            {
                "name": "engineer",
                "session_id": "session",
                "tags": ["agent:engineer"],
                "observations": [
                    {"type": "SPAN", "turn": 1, "start": start - 60_000, "end": start},
                    {"type": "SPAN", "turn": 2, "start": end, "end": end + 60_000},
                ],
            }
        ],
    }


def test_dependency_block_and_wake_cover_the_wait_between_turns(monkeypatch):
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    agent = SimpleNamespace(name="engineer", task="own", state="working", seat="")
    store = SimpleNamespace(redis=redis, agents=lambda slug: [agent])
    ledger = SimpleNamespace(tasks=lambda slug: [{"id": "own", "depends_on": ["dep"]}, {"id": "dep", "state": "pr"}])
    monkeypatch.setattr(cli, "_worker", lambda store, args: agent)
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    monkeypatch.setattr(cli, "now_ms", lambda: 60_001)
    cli.cmd_block(store, SimpleNamespace(slug="sw", note="waiting for dependency"))
    inbox = SimpleNamespace(send=lambda *args: None)
    waits.end_pass(store, "sw", {"dep": {"state": "done"}}, inbox, None, 12_539_000, None)

    held = idle.wait_history(redis, "sw", agent.name)
    assert held[0]["on"] == {"kind": "task", "target": "dep"}
    assert traces.turn_gaps(record(held), traces.Limits()) == []
    assert traces.turn_gaps(record([]), traces.Limits())[0].measure == 208


@pytest.mark.parametrize("kind", ["checks", "merge"])
def test_recorded_wait_covers_the_incident_despite_the_wake_margin(kind):
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    held = waits.on(kind, "https://github.com/org/repo/pull/1")
    held["queued"] = True
    idle.declare_wait(redis, "sw", "engineer", 1_792_369_612_462, "", 1_792_326_412_462, on=held)
    idle.end_wait(redis, "sw", "engineer", 1_792_334_747_090)
    history = idle.wait_history(redis, "sw", "engineer")

    assert traces.turn_gaps(record(history, 1_792_326_562_000, 1_792_334_750_000), traces.Limits()) == []


@pytest.mark.parametrize(
    "held, expected",
    [
        ([{"at": 120_000, "until": 12_480_000}], None),
        ([{"at": 1_860_000, "until": 3_660_000}], 178),
        ([{"at": 0, "until": 1}], 208),
        ([{"at": 12_540_000, "until": 13_000_000}], 208),
        ([{"at": 60_000, "until": 12_540_000}, {"at": 120_000, "until": 180_000}], None),
        ([{"at": 60_000, "until": 6_000_000}, {"at": 5_000_000, "until": 12_540_000}], None),
        ([{"at": 60_000, "until": 6_000_000}, {"at": 6_000_001, "until": 12_540_000}], None),
    ],
)
def test_only_uncovered_time_counts_towards_the_limit(held, expected):
    found = traces.turn_gaps(record(held), traces.Limits())
    assert [f.measure for f in found] == ([] if expected is None else [expected])


@pytest.mark.parametrize("uncovered", [1_800_000, 1_859_999, 1_860_000])
def test_uncovered_duration_keeps_the_whole_minute_threshold(uncovered):
    found = traces.turn_gaps(record([{"at": 60_000, "until": 12_540_000 - uncovered}]), traces.Limits())
    assert [f.measure for f in found] == ([31] if uncovered == 1_860_000 else [])
