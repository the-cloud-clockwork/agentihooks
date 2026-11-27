from types import SimpleNamespace

import pytest

from scripts.swarm import cli, idle, status
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


@pytest.mark.feature("balance-master-route")
def test_status_report_carries_the_route_only_on_the_front_door_master(
    saved: tuple[RedisStore, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, names = saved
    door = AgentRecord(
        "master@a1b2c3-0001",
        "master",
        "master",
        harness="claude",
        account="home",
        seat="master@sw",
        route_kind="master-login",
    )
    store.put_agent("sw", door)
    monkeypatch.setattr(status.HerdrRuntime, "live_names", lambda self: {*names.values(), door.name})

    report = {a["name"]: a["route"] for a in status.status_report(store, "sw", {"tasks": []})["agents"]}

    assert report == {door.name: "claude ai login", **dict.fromkeys(names.values(), "")}


@pytest.mark.feature("master-route-acting-front-door")
@pytest.mark.parametrize(("lead", "route"), [(False, "claude ai login"), (True, "")])
def test_status_report_carries_the_route_of_a_second_master_seat_acting_as_the_front_door(
    saved: tuple[RedisStore, dict[str, str]], monkeypatch: pytest.MonkeyPatch, lead: bool, route: str
) -> None:
    store, names = saved
    second = AgentRecord(
        "master@a1b2c3-0002", "master", "master", harness="claude", seat="master-2@sw", route_kind="master-login"
    )
    first = AgentRecord("master@a1b2c3-0001", "master", "master", harness="claude", seat="master@sw")
    for agent in [second, first] if lead else [second]:
        store.put_agent("sw", agent)
    monkeypatch.setattr(status.HerdrRuntime, "live_names", lambda self: {*names.values(), first.name, second.name})

    report = {a["name"]: a["route"] for a in status.status_report(store, "sw", {"tasks": []})["agents"]}

    assert report[second.name] == route


@pytest.mark.feature("master-route-acting-front-door")
def test_status_report_names_the_lead_seat_route_when_no_master_seat_is_held(
    saved: tuple[RedisStore, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, names = saved
    door = AgentRecord(
        "master@a1b2c3-0002", "master", "master", harness="claude", seat="master@sw", route_kind="master-login"
    )
    gone = AgentRecord("master@a1b2c3-0009", "master", "master", harness="claude", state="finished")
    for agent in (door, gone):
        store.put_agent("sw", agent)
    store.seats.occupy("master@sw", gone.name, 1)
    monkeypatch.setattr(status.HerdrRuntime, "live_names", lambda self: {*names.values(), door.name})

    report = {a["name"]: a["route"] for a in status.status_report(store, "sw", {"tasks": []})["agents"]}

    assert report[door.name] == "claude ai login"


def test_status_report_carries_only_the_latest_hundred_ended_lives(saved):
    import json

    store, _ = saved
    lives = [json.dumps({"name": f"life-{n}", "task": "t1", "ended_at": n}) for n in range(105)]
    store.redis.rpush(store.key("sw", "history"), *lives)

    history = status.status_report(store, "sw", {"tasks": []})["history"]
    assert [row["name"] for row in history] == [f"life-{n}" for n in range(5, 105)]


def test_status_report_carries_the_findings_it_is_given_without_finding_again(
    saved: tuple[RedisStore, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = saved
    calls: list[object] = []
    monkeypatch.setattr(status, "findings", lambda *args: calls.append(args) or [])
    found = [{"kind": "stale", "subject": "t1"}]

    report = status.status_report(store, "sw", {"tasks": []}, None, None, found)

    assert report["findings"] == found
    assert calls == []


def test_status_report_finds_the_findings_once_when_none_are_given(
    saved: tuple[RedisStore, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _ = saved
    calls: list[object] = []
    found = [{"kind": "stale", "subject": "t1"}]
    monkeypatch.setattr(status, "findings", lambda *args: calls.append(args) or found)

    report = status.status_report(store, "sw", {"tasks": []})

    assert report["findings"] == found
    assert len(calls) == 1


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


@pytest.mark.feature("swarm-status-agent-wait")
def test_status_text_prints_each_agents_declared_wait_kind_and_target(
    saved: tuple[RedisStore, dict[str, str]], capsys: pytest.CaptureFixture[str]
) -> None:
    store, names = saved
    lab = "https://github.com/the-cloud-clockwork/agentihooks/runs/123"
    at = status.now_ms()
    held = {"kind": "lab", "target": lab}
    idle.declare_wait(store.redis, "sw", names["claude"], at + 600_000, "lab grade", at, on=held)
    idle.declare_wait(store.redis, "sw", names["push"], at + 600_000, "a short pause", at)

    cli.cmd_status(store, SimpleNamespace(slug="sw", json=False))

    lines = capsys.readouterr().out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Agent\t"))
    head, *rows = [line.split("\t") for line in lines[start : start + len(names) + 1]]
    assert head[-1] == "Wait"
    shown = {row[0]: row[-1] for row in rows}
    assert {key: shown[name] for key, name in names.items()} == {
        "claude": f"lab {lab}",
        "push": "bare",
        "legacy": "-",
        "unknown": "-",
    }
