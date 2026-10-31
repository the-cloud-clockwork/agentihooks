"""Golden observations of the tick core and its fenced command entry point."""

import copy
import importlib
import json
import os
import re
import time
from dataclasses import asdict, is_dataclass, replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from scripts.swarm import cli
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from tests.swarm.test_delivery import FakeHerdr
from tests.swarm.test_tick import FakeLedger, FakeRuntime

tick_module = importlib.import_module("scripts.swarm.tick")
BASE_MS = 1_700_000_000_000
GOLDENS = Path(__file__).with_name("goldens.json")
SCENARIOS = (
    "running",
    "second-pass",
    "paused",
    "stopping",
    "stopped",
    "drained",
    "finished",
    "lost",
    "orphan",
    "failed-spawn",
    "full-capacity",
    "dependency",
)
LEDGER_WRITES = ("update_task", "notify", "mark_swarm", "rank_task", "comment", "join", "notice")
RUNTIME_WRITES = ("spawn", "retire", "reap_name", "nudge", "name_pane", "close_space", "close_pane")


def spy(target: Any, methods: tuple[str, ...], monkeypatch: pytest.MonkeyPatch) -> Mock:
    calls = Mock()
    for name in methods:
        wrapped = Mock(wraps=getattr(target, name))
        calls.attach_mock(wrapped, name)
        monkeypatch.setattr(target, name, wrapped)
    return calls


def canonical(value: Any, roots: tuple[str, ...]) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return canonical(asdict(value), roots)
    if isinstance(value, dict):
        return {str(canonical(k, roots)): canonical(v, roots) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [canonical(v, roots) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(canonical(v, roots) for v in value)
    if isinstance(value, Path):
        return canonical(str(value), roots)
    if isinstance(value, str):
        for root in roots:
            value = value.replace(root, "<fixture>")
        value = re.sub(r"test-[0-9a-f]+(?=:)", "test-run", value)
        if value.startswith(("{", "[")):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                return value
            return canonical(decoded, roots)
    return value


def redis_state(store: RedisStore) -> dict[str, Any]:
    client = store.redis
    readers = {
        "string": client.get,
        "hash": client.hgetall,
        "list": lambda key: client.lrange(key, 0, -1),
        "set": client.smembers,
        "zset": lambda key: client.zrange(key, 0, -1, withscores=True),
        "stream": client.xrange,
    }
    return {key: {"type": client.type(key), "value": readers[client.type(key)](key)} for key in sorted(client.keys())}


def calls_of(calls: Mock) -> list[Any]:
    return [[call[0], copy.deepcopy(call[1]), copy.deepcopy(call[2])] for call in calls.mock_calls]


def prepare(scenario: str, store: RedisStore, ledger: FakeLedger, runtime: FakeRuntime) -> None:
    if scenario in {"paused", "stopping", "stopped", "drained"}:
        store.update("sw", state=scenario)
    if scenario == "failed-spawn":
        runtime.fail = True
    if scenario == "full-capacity":
        runtime.full = True
    if scenario == "orphan":
        ledger.rows["t1"].update(state="claimed", claimed_by="engineer@a1b2c3-0099")
    if scenario == "dependency":
        ledger.rows["t1"]["depends_on"] = ["t2"]
        ledger.rows["t2"]["state"] = "blocked"
    if scenario in {"finished", "lost"}:
        agent = AgentRecord(
            "engineer@a1b2c3-0099", "eng", "t1", "w1:p1", started_at=BASE_MS - tick_module.STARTUP_GRACE_MS - 1
        )
        store.put_agent("sw", replace(agent, state="finished") if scenario == "finished" else agent)
        ledger.rows["t1"].update(state="done" if scenario == "finished" else "claimed", claimed_by=agent.name)
        if scenario == "finished":
            runtime.live.add(agent.name)


def replay(scenario: str, entry: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    import fakeredis

    from scripts.swarm import timing

    monkeypatch.setenv("AGENTIHOOKS_SWARM_SNAPSHOT_MINUTES", "0")
    monkeypatch.setenv("AGENTIHOOKS_MASTER_RETIRE_HANDOFF_MINUTES", "0")
    monkeypatch.setattr(time, "time", lambda: BASE_MS / 1000)
    monkeypatch.setattr(cli, "now_ms", lambda: BASE_MS)
    monkeypatch.setattr(timing, "time", Mock(monotonic=Mock(return_value=0.0)))
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=1))
    ledger = FakeLedger([{"id": "t1", "lane": "eng"}, {"id": "t2", "lane": "ci"}])
    runtime, messenger = FakeRuntime(), FakeHerdr({})
    prepare(scenario, store, ledger, runtime)
    ledger_calls = spy(ledger, LEDGER_WRITES, monkeypatch)
    runtime_calls = spy(runtime, RUNTIME_WRITES, monkeypatch)
    actions = []
    for offset in range(2 if scenario == "second-pass" else 1):
        now = BASE_MS + offset * 1000
        monkeypatch.setattr(cli, "now_ms", lambda: now)
        if entry == "tick":
            actions.append(tick_module.tick("sw", store, ledger, runtime, now_ms=now))
        else:
            actions.append(cli.run_tick(store, "sw", ledger, runtime, messenger))
    result = {
        "actions": actions,
        "redis": redis_state(store),
        "ledger_writes": calls_of(ledger_calls),
        "ledger_rows": ledger.rows,
        "runtime_writes": calls_of(runtime_calls),
        "messenger_prompts": messenger.prompts,
    }
    return canonical(result, (str(tmp_path), str(Path.home()), str(Path.cwd())))


def compare(record: dict[str, Any], scenario: str, entry: str) -> None:
    key = f"{entry}/{scenario}"
    expected = json.loads(GOLDENS.read_text()).get(key)
    assert record == expected, "GOLDEN_RECORD=" + json.dumps({key: record}, sort_keys=True)


@pytest.mark.feature("swarm-tick-characterisation")
@pytest.mark.parametrize("entry", ["tick", "run_tick"])
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_tick_matches_golden(scenario: str, entry: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    record = replay(scenario, entry, monkeypatch, tmp_path)
    compare(record, scenario, entry)


@pytest.mark.feature("swarm-tick-characterisation")
@pytest.mark.parametrize("entry", ["tick", "run_tick"])
def test_dropped_reap_action_breaks_golden(entry: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    original = tick_module._reap
    dropped = []

    def drop_action(*args: Any, **kwargs: Any) -> list[str]:
        actions = original(*args, **kwargs)
        assert actions, "finished scenario must exercise an observable reap action"
        dropped.append(actions[0])
        return actions[1:]

    monkeypatch.setattr(tick_module, "_reap", drop_action)
    record = replay("finished", entry, monkeypatch, tmp_path)
    assert dropped
    if os.environ.get("TICK_CHARACTERISATION_PLANT") == "1":
        compare(record, "finished", entry)
    else:
        with pytest.raises(AssertionError, match="GOLDEN_RECORD"):
            compare(record, "finished", entry)
