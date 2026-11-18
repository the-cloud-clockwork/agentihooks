"""The front door master refill in its deployed shape: after a recycle handoff the tick master step, and the night
watch at its default mode, place the successor through the pod controller's fenced runtime, the hive launch selection
and a launch order the root hive consumes, on root's claude ai login slot."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from tests.hive.test_controller_launch import setup as setup
from tests.hive.test_controller_launch import write_hive
from tests.swarm.client_class import client_class

MINUTE = 60_000
SEAT_MINUTES = 10
FIVE_HOUR_USED = 10.0
FIVE_HOUR_RESET_S = 3600
WEEK_RESET_S = 48 * 3600
WEEKS = (7.0, 80.0)
WEEK_IDS = ("login-at-seven-percent-week-as-on-17-november", "login-with-room")
HANDOFF = (
    "# Handoff v2\n## Intent\nKeep the front door.\n## Done\nNone\n## Stopped at\nNone\n## Next\nNone\n"
    "## Decisions and promises\nNone\n## Read first\nNone\n<!-- handoff complete -->\n"
)


def _root_hive(setup: SimpleNamespace, login_week_left: float) -> None:
    """Root, the one hive with a screen, offers the master claude ai login account beside a worker account, and the
    controller reads both accounts' quota through its fleet account route, as the deployed controller does."""
    from scripts.swarm import lease
    from scripts.swarm.profile_choice import ProfileDecision
    from scripts.swarm_v2 import quota
    from scripts.swarm_v2.kubernetes import approved

    now = lease.now_ms(setup.store)
    for account, week_left in (("account", 80.0), ("login", login_week_left)):
        observation = quota.Observation(
            account,
            "claude",
            "exe-" + "0" * 32,
            1,
            now,
            "allowed",
            FIVE_HOUR_USED,
            100.0 - week_left,
            now // 1000 + FIVE_HOUR_RESET_S,
            now // 1000 + WEEK_RESET_S,
        )
        setup.store.redis.hset(quota.latest_key("claude"), account, quota.encode(observation))
    observations = quota.QuotaObservations(setup.store, setup.config.slug, Mock(side_effect=AssertionError))
    route = approved.AccountRoute(lambda: frozenset({"account", "login"}), observations, dict, "claude")
    setup.herdr._quota_accounts = None
    setup.herdr.quota_accounts = approved.FleetAccounts(route, 6)
    setup.herdr._profile_decision.return_value = ProfileDecision("master", "lane", "master")
    write_hive(
        setup,
        id="root",
        excluded=[],
        ui="yes",
        master_login={"claude": "login"},
        slots=[
            {"harness": "claude", "account": "account", "kind": "subscription", "cap": 6},
            {"harness": "claude", "account": "login", "kind": "subscription", "cap": 4},
        ],
    )


def _recycled(setup: SimpleNamespace, now: int) -> str:
    """The front door master on root's login slot hands its seat off for a recycle through the swarm command."""
    from scripts.swarm import cli
    from scripts.swarm.store import AgentRecord
    from tests.swarm.test_tick import FakeLedger

    slug, started = setup.config.slug, now - 60 * MINUTE
    name = setup.store.next_name(slug, "master", started)
    old = AgentRecord(
        name,
        "master",
        "master",
        harness="claude",
        account="login",
        started_at=started,
        seat=f"master@{slug}",
        profile="master",
        route_kind="master-login",
    )
    setup.store.put_agent(slug, old)
    setup.store.seats.occupy(old.seat, name, started)
    cli._hand_off(setup.store, slug, old, HANDOFF, "recycle", FakeLedger([]))
    return name


def _controller(setup: SimpleNamespace) -> Any:
    """The pod controller's fenced runtime; root consumes each launch order while the controller waits on it."""
    from scripts.hive import orders
    from scripts.swarm import controller, tick
    from scripts.swarm_v2.runtime.base import Outcome, RuntimeRouter, Status
    from scripts.swarm_v2.runtime.routed import RoutedRuntime

    setup.herdr.live_names.return_value = set()
    routed = RoutedRuntime(setup.herdr, RuntimeRouter([]))
    fenced = controller.FencedRuntime(setup.store, setup.config.slug, setup.held, routed, "compose")
    root = Mock()
    root.spawn.return_value = Outcome(
        "spawn",
        Status.OK,
        "local",
        tick.Placed("pane", "claude", "login", profile="master", profile_decision={"validation": {"pid": 12}}),
    )
    root.namespace.return_value = "namespace"
    root.table.return_value = {12: SimpleNamespace(start_time=34)}
    consumer = orders.Consumer(setup.store.redis, "root", root)
    launch = fenced.runtime.hive_launch
    launch.opening = lambda request: f"opening for {request.name}"
    launch.sleep = lambda _: consumer.poll()
    return fenced


def _deployed(setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, login_week_left: float) -> SimpleNamespace:
    """A controller pod with no workstation herdr, root offering the login slot with ``login_week_left`` percent of
    its week, a front door master just recycled, and a ledger client recording each master join and chat line."""
    from scripts import master_claude_login
    from scripts.swarm import controller, lease, ledger_client

    joined: list[tuple[Any, ...]] = []
    said: list[tuple[Any, ...]] = []
    monkeypatch.setenv(controller.POD_NAME, "agentihooks-swarm-controller-0")
    monkeypatch.setenv("SWARM_HIVE_ID", "controller-pod")
    monkeypatch.delenv("AGENTIHOOKS_MASTER_DOWN_MINUTES", raising=False)
    monkeypatch.setattr(master_claude_login.hooks_config, "MASTER_CLAUDE_LOGIN", True)
    monkeypatch.setattr(master_claude_login, "unusable", lambda: "")
    monkeypatch.setattr(controller, "_workstation_herdr", lambda memo=None: False)
    client = SimpleNamespace(join=lambda *args: joined.append(args), say=lambda *args: said.append(args))
    monkeypatch.setattr(ledger_client, "LedgerClient", client_class(client))
    now = lease.now_ms(setup.store)
    _root_hive(setup, login_week_left)
    old = _recycled(setup, now)
    runtime = _controller(setup)
    return SimpleNamespace(slug=setup.config.slug, now=now, old=old, runtime=runtime, joined=joined, said=said)


def _successor(setup: SimpleNamespace, shape: SimpleNamespace) -> Any:
    """The one master after the recycled one, checked on root's login slot through root's launch order."""
    from scripts.hive import orders

    agents = setup.store.agents(shape.slug)
    (successor,) = [agent for agent in agents if agent.lane == "master" and agent.name != shape.old]
    assert (successor.seat, successor.hive, successor.account, successor.route_kind) == (
        f"master@{shape.slug}",
        "root",
        "login",
        "master-login",
    )
    raw = setup.store.redis.hget(setup.store.key(shape.slug, "controller-launch-orders"), "master")
    order = orders.LaunchOrder(**json.loads(raw))
    assert (order.hive, order.slot, order.route_kind) == ("root", "login", "master-login")
    assert (shape.joined, shape.said) == ([(shape.slug, successor.name, "orchestrator")], [])
    return successor


def _ledger() -> Any:
    """The tick test's fake ledger for the fixture swarm: it records each notice the master step posts as its swarm and
    text, and each alert the night watch raises as its source and text."""
    from tests.swarm.test_tick import FakeLedger

    ledger: Any = FakeLedger([])
    ledger.alerts = []
    ledger.notify = lambda slug, text: ledger.notes.append((slug, text))
    ledger.raise_alert = lambda slug, source, text: ledger.alerts.append((source, text))
    return ledger


@pytest.mark.feature("front-door-master-refill-deployed-shape")
@pytest.mark.parametrize("login_week_left", WEEKS, ids=WEEK_IDS)
def test_a_recycled_front_door_master_is_refilled_on_the_root_login_slot_before_the_forcing_window(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, login_week_left: float
) -> None:
    from scripts.swarm import controller, tick, tick_master
    from scripts.tick.steps import masters

    shape = _deployed(setup, monkeypatch, login_week_left)
    ledger = _ledger()
    window = tick_master.down_minutes(os.environ)
    passes: list[list[str]] = []
    for minute in range(int(window) + 1):
        values = {
            "slug": shape.slug,
            "store": setup.store,
            "ledger": ledger,
            "runtime": shape.runtime,
            "now_ms": shape.now + minute * MINUTE,
            "config": setup.store.config(shape.slug),
            "doc": {"tasks": []},
        }
        passes.append(masters._tick_master(tick, values.__getitem__))
        if any(action.startswith("spawned master") for action in passes[-1]):
            break
    refused = controller.WORKSTATION.format(lane="master")
    assert [action for actions in passes for action in actions if action.startswith(refused)] == []
    assert ledger.notes == []
    successor = _successor(setup, shape)
    assert passes[-1] == [f"spawned master {successor.name}"]
    assert len(passes) - 1 < window


@pytest.mark.feature("night-watch-respawns-by-default")
@pytest.mark.parametrize("login_week_left", WEEKS, ids=WEEK_IDS)
def test_the_default_night_watch_refills_a_recycled_front_door_seat_at_ten_minutes_through_the_pod_controller(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, login_week_left: float
) -> None:
    from scripts.gates import log as gate_log
    from scripts.swarm import capacity
    from scripts.swarm.health import merge_stall
    from scripts.tick.steps import masters
    from tests.swarm.test_night_watch import QUIET

    shape = _deployed(setup, monkeypatch, login_week_left)
    ledger = _ledger()
    monkeypatch.setattr(gate_log, "swarm_home", lambda: tmp_path)
    monkeypatch.setattr(merge_stall, "observe", lambda repo, at: {**QUIET, "at": at})
    monkeypatch.setattr(capacity, "ready_work", lambda slug, store, doc: ({}, {"eng": [], "ci": [], "plan": []}))
    assert "nightwatch" not in setup.store.config(shape.slug).gates
    passes = [
        masters._night_watch(
            shape.slug, setup.store, ledger, shape.runtime, {"tasks": []}, shape.now + minute * MINUTE, False
        )
        for minute in range(0, SEAT_MINUTES + 1, 2)
    ]
    successor = _successor(setup, shape)
    assert passes[:-1] == [[]] * (SEAT_MINUTES // 2)
    assert passes[-1][0] == f"spawned master {successor.name}"
    assert [source for source, _ in ledger.alerts] == ["seat"]
