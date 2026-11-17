"""The front door master refill in its deployed shape: after a recycle handoff the tick master step places the
successor through the pod controller's fenced runtime, the hive launch selection and a launch order the root hive
consumes, on root's claude ai login slot, before the forcing window ends."""

import json
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from tests.hive.test_controller_launch import setup as setup
from tests.hive.test_controller_launch import write_hive

MINUTE = 60_000
HANDOFF = (
    "# Handoff v2\n## Intent\nKeep the front door.\n## Done\nNone\n## Stopped at\nNone\n## Next\nNone\n"
    "## Decisions and promises\nNone\n## Read first\nNone\n<!-- handoff complete -->\n"
)


def _root_hive(setup: SimpleNamespace) -> None:
    """Root, the one hive with a screen, offers the master claude ai login account beside a worker account."""
    from scripts.swarm.capacity import Account
    from scripts.swarm.profile_choice import ProfileDecision

    setup.herdr._profile_decision.return_value = ProfileDecision("master", "lane", "master")
    setup.herdr._quota_accounts = [
        Account("claude", "account", "room", 0, 80, 80, 6),
        Account("claude", "login", "room", 3, 80, 80, 6),
    ]
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
        hive="root",
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


def test_a_recycled_front_door_master_is_refilled_on_the_root_login_slot_before_the_forcing_window(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import master_claude_login
    from scripts.hive import orders
    from scripts.swarm import controller, lease, ledger_client, tick, tick_master
    from scripts.tick.steps import masters
    from tests.swarm.test_tick import FakeLedger

    joined: list[tuple[Any, ...]] = []
    said: list[tuple[Any, ...]] = []
    monkeypatch.setenv(controller.POD_NAME, "agentihooks-swarm-controller-0")
    monkeypatch.setenv("SWARM_HIVE_ID", "controller-pod")
    monkeypatch.delenv("AGENTIHOOKS_MASTER_DOWN_MINUTES", raising=False)
    monkeypatch.setattr(master_claude_login.hooks_config, "MASTER_CLAUDE_LOGIN", True)
    monkeypatch.setattr(master_claude_login, "unusable", lambda: "")
    monkeypatch.setattr(controller, "_workstation_herdr", lambda memo=None: False)
    client = SimpleNamespace(join=lambda *args: joined.append(args), say=lambda *args: said.append(args))
    monkeypatch.setattr(ledger_client, "LedgerClient", lambda **_: client)
    slug, now = setup.config.slug, lease.now_ms(setup.store)
    _root_hive(setup)
    old = _recycled(setup, now)
    fenced = _controller(setup)
    window = tick_master.down_minutes(os.environ)
    passes: list[list[str]] = []
    for minute in range(int(window) + 1):
        values = {
            "slug": slug,
            "store": setup.store,
            "ledger": FakeLedger([]),
            "runtime": fenced,
            "now_ms": now + minute * MINUTE,
            "config": setup.store.config(slug),
            "doc": {"tasks": []},
        }
        passes.append(masters._tick_master(tick, values.__getitem__))
        if any(action.startswith("spawned master") for action in passes[-1]):
            break
    refused = controller.WORKSTATION.format(lane="master")
    assert [action for actions in passes for action in actions if action.startswith(refused)] == []
    (successor,) = [agent for agent in setup.store.agents(slug) if agent.lane == "master" and agent.name != old]
    assert passes[-1] == [f"spawned master {successor.name}"]
    assert len(passes) - 1 < window
    assert (successor.seat, successor.hive, successor.account, successor.route_kind) == (
        f"master@{slug}",
        "root",
        "login",
        "master-login",
    )
    raw = setup.store.redis.hget(setup.store.key(slug, "controller-launch-orders"), "master")
    order = orders.LaunchOrder(**json.loads(raw))
    assert (order.hive, order.slot, order.route_kind) == ("root", "login", "master-login")
    assert (joined, said) == ([(slug, successor.name, "orchestrator")], [])
