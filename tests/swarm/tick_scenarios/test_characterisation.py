"""Golden observations of the tick core and its fenced command entry point."""

import copy
import importlib
import json
import os
import re
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass, replace
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from scripts.swarm import cli
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.tick import engine, registry
from tests.swarm.test_delivery import FakeHerdr
from tests.swarm.test_tick import FakeLedger, FakeRuntime

tick_module = importlib.import_module("scripts.swarm.tick")
agent_steps = importlib.import_module("scripts.tick.steps.agents")
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
    "failed-spawn",
    "full-capacity",
    "dependency",
    "interval",
    "interval-slow",
    "hive-roll",
)
PASSES = {"hive-roll": (2, 60_000), "second-pass": (2, 1000), "interval": (2, 60_000), "interval-slow": (3, 60_000)}
CADENCES = {"interval-slow": "3"}
LEDGER_WRITES = ("update_task", "notify", "mark_swarm", "rank_task", "comment", "join", "notice")
RUNTIME_WRITES = ("spawn", "retire", "reap_name", "nudge", "name_pane", "close_space", "close_pane")


def spy(target: Any, methods: tuple[str, ...], monkeypatch: pytest.MonkeyPatch) -> Mock:
    calls = Mock()
    for name in methods:
        method = getattr(target, name)
        wrapped = Mock(wraps=method)
        wrapped.__qualname__ = method.__qualname__
        wrapped.__name__ = method.__name__
        wrapped.__module__ = method.__module__
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


def step_rows(appended: Mock) -> list[dict[str, list[Any]]]:
    """Return each pass's step outcome and rested flag, a finished step over its wall clock budget read as ok."""
    passes: dict[str, dict[str, list[Any]]] = {}
    for call in appended.call_args_list:
        if call.args[2] == "tick:records":
            row = call.args[3]
            late = row["outcome"] == engine.LATE and not row["abandoned"]
            passes.setdefault(row["pass_id"], {})[row["step"]] = [engine.OK if late else row["outcome"], row["rested"]]
    return list(passes.values())


def prepare_hive_roll(store: RedisStore, ledger: FakeLedger) -> None:
    from scripts.hive import reports

    for index in range(10):
        task_id = f"rolled-{index}"
        agent = store.start_execution(
            "sw",
            AgentRecord(
                store.next_name("sw", "eng"),
                "eng",
                task_id,
                seat=f"eng-{index + 1}@sw",
                hive="rolled-hive",
                started_at=BASE_MS - tick_module.STARTUP_GRACE_MS - 1,
            ),
        )
        ledger.rows[task_id] = {
            "id": task_id,
            "state": "claimed",
            "claimed_by": agent.name,
            "lane": "eng",
            "out_of_scope": False,
        }
    store.redis.set(reports.key("rolled-hive"), json.dumps({"names": [], "started_at": BASE_MS - 1}))


def prepare(scenario: str, store: RedisStore, ledger: FakeLedger, runtime: FakeRuntime) -> None:
    if scenario in {"paused", "stopping", "stopped", "drained"}:
        store.update("sw", state=scenario)
    if scenario == "hive-roll":
        prepare_hive_roll(store, ledger)
    if scenario == "failed-spawn":
        runtime.fail = True
    if scenario == "full-capacity":
        runtime.full = True
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


def github_reply(repo: str, args: list[str]) -> dict[str, Any]:
    if args[0] == "repo":
        return {"nameWithOwner": "fixture/repo"}
    if args[1] == "graphql":
        return {"data": {"repository": {"mergeQueue": {"entries": {"totalCount": 0}}}}}
    return {"workflow_runs": []}


def replay(scenario: str, entry: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    import fakeredis

    from scripts.swarm import commands, incidents, panic, reboot, timing
    from scripts.swarm.health import merge_stall

    monkeypatch.setattr(merge_stall, "request", github_reply)

    monkeypatch.setattr(commands, "hive_id", lambda: "fixture-hive")
    monkeypatch.setattr(incidents.socket, "gethostname", lambda: "fixture-host")
    monkeypatch.setattr(reboot, "boot_id", lambda: "00000000-0000-0000-0000-000000000001")
    pass_identities = count(1)
    monkeypatch.setattr(panic, "uuid", SimpleNamespace(uuid4=lambda: uuid.UUID(int=next(pass_identities))))
    identities = count(1)
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(int=next(identities)))
    for module in ("execution", "health.verdicts", "launch_evidence", "promotion_owner", "incidents"):
        monkeypatch.setattr(importlib.import_module(f"scripts.swarm.{module}"), "uuid4", uuid.uuid4)

    monkeypatch.setenv("AGENTIHOOKS_SWARM_SNAPSHOT_MINUTES", "0")
    monkeypatch.setenv("AGENTIHOOKS_MASTER_RETIRE_HANDOFF_MINUTES", "0")
    monkeypatch.setattr(time, "time", lambda: BASE_MS / 1000)
    monkeypatch.setattr(cli, "now_ms", lambda: BASE_MS)
    clock = Mock(
        monotonic=Mock(return_value=0.0),
        perf_counter=Mock(return_value=0.0),
        time_ns=Mock(return_value=BASE_MS * 1000000),
    )
    monkeypatch.setattr(timing, "time", clock)
    if cadence := CADENCES.get(scenario):
        monkeypatch.setenv("AGENTIHOOKS_TICK_CADENCE", cadence)
    appended = Mock()
    monkeypatch.setattr(timing, "append_record", appended)
    monkeypatch.setattr(
        timing, "append_step", lambda store, slug, record: appended(store, slug, "tick:steps", record), raising=False
    )
    store = RedisStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=1))
    ledger = FakeLedger([{"id": "t1", "lane": "eng"}, {"id": "t2", "lane": "ci"}])
    runtime, messenger = FakeRuntime(), FakeHerdr({})
    prepare(scenario, store, ledger, runtime)
    ledger_calls = spy(ledger, LEDGER_WRITES, monkeypatch)
    runtime_calls = spy(runtime, RUNTIME_WRITES, monkeypatch)
    actions = []
    passes, gap_ms = PASSES.get(scenario, (1, 1000))
    for offset in range(passes):
        now = BASE_MS + offset * gap_ms
        monkeypatch.setattr(cli, "now_ms", lambda: now)
        if scenario in CADENCES or scenario == "interval":
            monkeypatch.setattr(time, "time", lambda: now / 1000)
            clock.time_ns.return_value = now * 1000000
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
    if scenario.startswith("interval"):
        result["step_rows"] = step_rows(appended)
    return canonical(result, (str(tmp_path), str(Path.home()), str(Path.cwd())))


def compare(record: dict[str, Any], scenario: str, entry: str) -> None:
    key = f"{entry}/{scenario}"
    expected = json.loads(GOLDENS.read_text()).get(key)
    assert record == expected, "GOLDEN_RECORD=" + json.dumps({key: record}, sort_keys=True)


@pytest.mark.feature("swarm-tick-dead-remote-budget")
@pytest.mark.parametrize("entry", ["tick", "run_tick"])
def test_a_hive_roll_drops_ten_seats_and_launches_within_two_budgeted_passes(
    entry: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scripts.swarm import fencing
    from scripts.tick.deadline import Deadline

    elapsed = [0.0]
    dropping: ContextVar[bool] = ContextVar("dropping", default=False)
    dropped: list[float] = []
    reaped: list[float] = []
    drop, reap, read = tick_module._drop_remote, agent_steps._reap, fencing.read

    def clock() -> float:
        return elapsed[0]

    def deadline(cls: type[Deadline], seconds: float, *, clock: Callable[[], float] = clock) -> Deadline:
        return cls(clock() + seconds, clock)

    def archive_read(store: Any, slug: str) -> dict[str, Any]:
        if dropping.get():
            elapsed[0] += 3.0
        return read(store, slug)

    def measured_drop(*args: Any, **kwargs: Any) -> str:
        started = clock()
        token = dropping.set(True)
        try:
            action: str = drop(*args, **kwargs)
        finally:
            dropping.reset(token)
        dropped.append(clock() - started)
        return action

    def measured_reap(*args: Any, **kwargs: Any) -> list[str]:
        started = clock()
        actions: list[str] = reap(*args, **kwargs)
        reaped.append(clock() - started)
        return actions

    monkeypatch.setattr(Deadline, "after", classmethod(deadline))
    monkeypatch.setattr(fencing, "read", archive_read)
    monkeypatch.setattr(tick_module, "_drop_remote", measured_drop)
    monkeypatch.setattr(agent_steps, "_reap", measured_reap)
    record = replay("hive-roll", entry, monkeypatch, tmp_path)
    assert len(dropped) == 10
    assert max(dropped) < 5.0
    assert len(reaped) == 2
    assert max(reaped) < 22.0
    assert any(action.startswith("spawned engineer") for actions in record["actions"][:2] for action in actions)


@pytest.mark.feature("swarm-tick-characterisation")
@pytest.mark.parametrize("entry", ["tick", "run_tick"])
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_tick_matches_golden(scenario: str, entry: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    record = replay(scenario, entry, monkeypatch, tmp_path)
    if output := os.environ.get("TICK_CHARACTERISATION_OUTPUT"):
        directory = Path(output)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{entry}-{scenario}.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    compare(record, scenario, entry)


@pytest.mark.feature("swarm-tick-declared-reads")
@pytest.mark.parametrize("entry", ["tick", "run_tick"])
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_recorded_passes_read_only_declared_values(
    scenario: str, entry: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scripts.tick import sites
    from scripts.tick import steps as tick_steps

    refused: list[tuple[str, str]] = []
    wrote: list[tuple[str, str]] = []
    original = tick_steps.refuse

    def record(step: str, key: str) -> None:
        refused.append((step, key))
        original(step, key)

    def guarded(owner_hooks: Callable[[Any], dict[str, Callable[[], bool]]], table: dict[str, Any]) -> Any:
        def hooks(owner: Any) -> dict[str, Callable[[], bool]]:
            return {site: guard(owner, site, hook, table[site]) for site, hook in owner_hooks(owner).items()}

        return hooks

    def guard(owner: Any, site: str, hook: Callable[[], bool], keys: Any) -> Callable[[], bool]:
        reads, writes = keys
        allowed = tick_steps.declared_keys(reads | writes)

        def run() -> bool:
            real = owner.box
            before = dict(real)
            owner.box = tick_steps.DeclaredBox(real, f"hook {site}", allowed)
            try:
                return hook()
            finally:
                owner.box = real
                changed = {key for key in real if key not in before or real[key] is not before[key]}
                wrote.extend((site, key) for key in sorted(changed - tick_steps.declared_keys(writes)))

        return run

    monkeypatch.setattr(tick_steps, "refuse", record)
    monkeypatch.setattr(cli._Wrapper, "hooks", guarded(cli._Wrapper.hooks, cli.WRAPPER_HOOK_KEYS))
    monkeypatch.setattr(sites.TickSites, "hooks", guarded(sites.TickSites.hooks, sites.TICK_HOOK_KEYS))
    replay(scenario, entry, monkeypatch, tmp_path)
    assert (refused, wrote) == ([], [])


@pytest.mark.feature("swarm-tick-characterisation")
@pytest.mark.parametrize("entry", ["tick", "run_tick"])
def test_dropped_reap_action_breaks_golden(entry: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    original = agent_steps._reap
    dropped = []

    def drop_action(*args: Any, **kwargs: Any) -> list[str]:
        actions = original(*args, **kwargs)
        assert actions, "finished scenario must exercise an observable reap action"
        dropped.append(actions[0])
        return actions[1:]

    monkeypatch.setattr(agent_steps, "_reap", drop_action)
    record = replay("finished", entry, monkeypatch, tmp_path)
    assert dropped
    if os.environ.get("TICK_CHARACTERISATION_PLANT") == "1":
        compare(record, "finished", entry)
    else:
        with pytest.raises(AssertionError, match="GOLDEN_RECORD"):
            compare(record, "finished", entry)


class ReadCount:
    """Ledger reads of one run_tick pass held through a counted view: whole reads, delta reads and hierarchy reads."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, second_read: bool = False) -> None:
        """Count the fake ledger's reads and serve the pass a held view whose delta reads are counted."""
        from scripts.tick import view

        self.counts: dict[str, int] = {"state": 0, "changes": 0, "hierarchy": 0, "writes": 0}
        self.unwritten: list[int] = []
        self.written = True
        whole, tree = FakeLedger.state, FakeLedger.hierarchy

        def state(ledger: FakeLedger, slug: str) -> dict[str, Any]:
            self.counts["state"] += 1
            return whole(ledger, slug)

        def hierarchy(ledger: FakeLedger, slug: str) -> list[dict[str, Any]]:
            self.counts["hierarchy"] += 1
            return tree(ledger, slug)

        monkeypatch.setattr(FakeLedger, "state", state)
        monkeypatch.setattr(FakeLedger, "hierarchy", hierarchy)
        for name in LEDGER_WRITES:
            monkeypatch.setattr(FakeLedger, name, self.writing(getattr(FakeLedger, name), view.wrote))

        def hold(beating: Any, slug: str, ledger: FakeLedger) -> None:
            beating.enter_context(view.holding())

            def renew() -> view.LedgerView:
                if not self.written:
                    self.unwritten.append(self.counts["changes"])
                self.counts["changes"] += 1
                self.written = False
                return view.LedgerView(self.counts["changes"], copy.deepcopy(whole(ledger, slug)))

            held = renew()
            view.CURRENT.set(held)
            view.HELD.set(view.Held(held, renew))
            view.REFRESH.set((slug, renew))
            if second_read:
                view.wrote()

        monkeypatch.setattr(cli, "_hold_view", hold)

    def writing(self, method: Any, wrote: Any) -> Any:
        """``method`` counted as a ledger write that leaves the held view stale, as the ledger client does."""

        def write(ledger: FakeLedger, *args: Any, **kwargs: Any) -> Any:
            found = method(ledger, *args, **kwargs)
            self.counts["writes"] += 1
            self.written = True
            wrote()
            return found

        write.__name__ = write.__qualname__ = method.__name__
        return write


@pytest.mark.feature("swarm-tick-delta-reads")
def test_a_running_pass_reads_the_ledger_once_plus_once_per_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    reads = ReadCount(monkeypatch)
    replay("running", "run_tick", monkeypatch, tmp_path)
    assert reads.counts["hierarchy"] == 0, reads.counts
    assert reads.counts["state"] == 0, reads.counts
    assert 1 <= reads.counts["changes"] <= 1 + reads.counts["writes"], reads.counts
    assert reads.unwritten == [], reads.counts


@pytest.mark.feature("swarm-tick-delta-reads")
def test_a_planted_second_read_without_a_write_is_counted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    reads = ReadCount(monkeypatch, second_read=True)
    replay("running", "run_tick", monkeypatch, tmp_path)
    assert reads.unwritten, reads.counts


@pytest.mark.feature("fenced-live-names-listing")
def test_running_pass_lists_live_names_once_plus_once_per_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    listings: list[int] = []
    original = FakeRuntime.live_names

    def counted(self: FakeRuntime) -> set[str]:
        listings.append(1)
        return original(self)

    monkeypatch.setattr(FakeRuntime, "live_names", counted)
    record = replay("running", "run_tick", monkeypatch, tmp_path)
    launches = sum(1 for call in record["runtime_writes"] if call[0] == "spawn")
    assert (len(listings), launches) == (3, 3), f"LIVE_NAMES_LISTINGS={len(listings)} LAUNCHES={launches}"


def assert_rests(record: dict[str, Any], gap_s: float, lane: str | None) -> set[str]:
    cadence = engine.cadence_s()
    wide = {
        step.name: engine.interval_s(step, cadence)
        for step in registry.steps()
        if engine.interval_s(step, cadence) > engine.CRON_S and (lane is None or step.lane == lane)
    }
    first, *later = record["step_rows"]
    ran = {name for name, (outcome, rested) in first.items() if name in wide and not rested}
    ran &= {name for name, (outcome, _) in first.items() if outcome in (engine.OK, engine.FAILED)}
    assert ran == set(wide), f"the first pass left {sorted(set(wide) - ran)} unrun"
    always = set(ran)
    for index, rows in enumerate(later, start=1):
        expected = {name for name in ran if index * gap_s <= wide[name] - engine.CRON_S}
        rested = {name for name in ran if rows.get(name, ["", False])[1]}
        assert rested == expected, f"pass {index + 1} rested {sorted(rested)}, expected {sorted(expected)}"
        always &= rested
    return always


@pytest.mark.feature("swarm-tick-step-isolation")
def test_interval_scenario_runs_each_wide_step_once_and_rests_it_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = replay("interval", "run_tick", monkeypatch, tmp_path)
    assert len(record["step_rows"]) == 2
    assert assert_rests(record, 60.0, None)


@pytest.mark.feature("swarm-tick-step-isolation")
def test_interval_slow_scenario_runs_a_slow_step_once_in_three_passes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = replay("interval-slow", "run_tick", monkeypatch, tmp_path)
    assert len(record["step_rows"]) == 3
    assert assert_rests(record, 60.0, engine.SLOW)


@pytest.mark.feature("swarm-tick-step-isolation")
@pytest.mark.parametrize(("scenario", "gap_s", "lane"), [("interval", 60.0, None), ("interval-slow", 60.0, "slow")])
def test_engine_ignoring_the_last_run_hash_breaks_the_interval_scenarios(
    scenario: str, gap_s: float, lane: str | None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from scripts.swarm import timing

    monkeypatch.setattr(timing, "step_ends", lambda store, slug: {})
    record = replay(scenario, "run_tick", monkeypatch, tmp_path)
    with pytest.raises(AssertionError, match="rested"):
        assert_rests(record, gap_s, lane)
