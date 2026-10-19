from types import SimpleNamespace

import pytest

from scripts.swarm import cli, status
from scripts.swarm.store import APP_SERVER_TRANSPORT, LEGACY_TRANSPORT, AgentRecord, RedisStore, SwarmConfig
from tests.swarm.test_tick import FakeLedger


@pytest.fixture
def saved(monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/r", 4, 0))
    names = {}
    for slot, (key, harness, transport) in enumerate(
        [("claude", "claude", ""), ("push", "codex", APP_SERVER_TRANSPORT), ("legacy", "codex", LEGACY_TRANSPORT)],
        start=1,
    ):
        name = store.next_name("sw", "eng", 1000 + slot)
        agent = AgentRecord(name, "eng", f"t{slot}", harness=harness, seat=f"eng-{slot}@sw", transport=transport)
        names[key] = store.start_execution("sw", agent).name
    names["unknown"] = "old"
    store.put_agent("sw", AgentRecord("old", "ci", "t4", harness=""))
    monkeypatch.setattr(cli, "LedgerClient", lambda: FakeLedger([]))
    monkeypatch.setattr(status.HerdrRuntime, "live_names", lambda self: set(names.values()))
    return store, names


def test_status_report_carries_each_agents_transport_capabilities(saved):
    store, names = saved
    report = {a["name"]: a["capabilities"] for a in status.status_report(store, "sw", {"tasks": []})["agents"]}
    assert {key: report[name] for key, name in names.items()} == {
        "claude": {"push_inbox": True, "native_terminal_attach": False},
        "push": {"push_inbox": True, "native_terminal_attach": True},
        "legacy": {"push_inbox": False, "native_terminal_attach": False},
        "unknown": {"push_inbox": False, "native_terminal_attach": False},
    }


def test_status_text_prints_push_inbox_and_native_terminal_per_agent(saved, capsys):
    store, names = saved
    cli.cmd_status(store, SimpleNamespace(slug="sw", json=False))
    lines = capsys.readouterr().out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Agent\t"))
    head, *rows = [line.split("\t") for line in lines[start : start + len(names) + 1]]
    push, attach = head.index("Push inbox"), head.index("Native terminal")
    shown = {row[0]: (row[push], row[attach]) for row in rows}
    assert {key: shown[name] for key, name in names.items()} == {
        "claude": ("yes", "no"),
        "push": ("yes", "yes"),
        "legacy": ("no", "no"),
        "unknown": ("no", "no"),
    }
