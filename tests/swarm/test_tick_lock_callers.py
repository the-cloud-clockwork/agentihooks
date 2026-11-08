"""Each tick lock holder names itself: the pass, its stop release, its steps, a rename and a master placement."""

import json
from collections.abc import Callable
from contextvars import ContextVar, Token
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import cli, controller, tick_stop, timing
from scripts.swarm.rename import LOCK_MS, rename_swarm
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
from tests.swarm.test_cli import env, run
from tests.swarm.test_delivery import FakeHerdr
from tests.swarm.test_tick_watch import FRONT_DOOR, START, agent, store

__all__ = ["env", "store"]

pytestmark = pytest.mark.unit

Capture = pytest.CaptureFixture[str]


class StepHook:
    """Records the hook the pass sets before each step while still serving it."""

    def __init__(self) -> None:
        self.hooks: list[Callable[[], None]] = []
        self.var: ContextVar[Callable[[], None] | None] = ContextVar("tick_before_step_seen", default=None)

    def set(self, hook: Callable[[], None]) -> Token[Callable[[], None] | None]:
        self.hooks.append(hook)
        return self.var.set(hook)

    def reset(self, token: Token[Callable[[], None] | None]) -> None:
        self.var.reset(token)

    def get(self) -> Callable[[], None] | None:
        return self.var.get()


def journal(capsys: Capture, *callers: str) -> list[tuple[str, str, str]]:
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    return [
        (line["slug"], line["caller"], line["outcome"])
        for line in lines
        if line.get("event") == "swarm_tick_lock" and line["caller"] in callers
    ]


def test_the_pass_names_itself_on_take_step_keep_release_and_stop_release(
    env: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch, capsys: Capture
) -> None:
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    take, hold = controller.take_tick_lock, tick_stop.hold
    taken: list[tuple[Any, ...]] = []
    kept: list[tuple[Any, ...]] = []
    held: list[Callable[[], None]] = []
    hook = StepHook()

    def taking(*args: Any) -> str | None:
        taken.append(args[3:])
        return take(*args)

    def holding(token: str, release: Callable[[], None]) -> None:
        held.append(release)
        hold(token, release)

    monkeypatch.setattr(controller, "take_tick_lock", taking)
    monkeypatch.setattr(controller, "keep_tick", lambda *args: kept.append(args[4:]))
    monkeypatch.setattr(tick_stop, "hold", holding)
    monkeypatch.setattr(timing, "BEFORE_STEP", hook)
    capsys.readouterr()
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert taken[0] == (cli.TICK_LOCK_MS, "pass")
    kept.clear()
    hook.hooks[0]()
    assert kept == [(cli.TICK_LOCK_MS, "pass step")]
    held[0]()
    assert journal(capsys, "pass", "pass stop") == [
        ("sw", "pass", "taken"),
        ("sw", "pass", "released"),
        ("sw", "pass stop", "found another holder"),
    ]


@pytest.fixture
def renaming() -> RedisStore:
    import fakeredis

    renamed = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    renamed.create(SwarmConfig("sw", "/repo", 1, 0))
    renamed.put_agent("sw", AgentRecord("sw-eng-1", "eng", "t1", pane_id="w1:p1"))
    return renamed


def test_a_rename_names_itself_on_every_keep_and_on_release(
    renaming: RedisStore, monkeypatch: pytest.MonkeyPatch, capsys: Capture
) -> None:
    panes = {"agents": [{"pane_id": "w1:p1", "name": "sw-eng-1", "workspace_id": "w1"}], "workspaces": []}
    keep = controller.keep_tick
    kept: list[tuple[Any, ...]] = []

    def keeping(*args: Any) -> None:
        kept.append(args[4:])
        keep(*args)

    monkeypatch.setattr(controller, "keep_tick", keeping)
    rename_swarm(
        renaming,
        "sw",
        SimpleNamespace(rename_agent=lambda slug, old, new: None),
        SimpleNamespace(herdr=lambda a: panes),
        10,
    )
    assert kept and set(kept) == {(LOCK_MS, "rename")}
    assert journal(capsys, "rename") == [("sw", "rename", "taken"), ("sw", "rename", "released")]


def test_place_master_names_itself_on_take_and_release(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, capsys: Capture
) -> None:
    from scripts.swarm import tick
    from tests.swarm.test_tick import FakeRuntime

    monkeypatch.setenv("SWARM_HIVE_ID", "home")
    agent(store, "master@a1b2c3-0001", FRONT_DOOR, lane=MASTER, state="finished")
    tick.place_master("sw", store, FakeRuntime(), START)
    assert journal(capsys, "place master", "PLACE MASTER") == [
        ("sw", "place master", "taken"),
        ("sw", "place master", "released"),
    ]
