import json
from dataclasses import replace

import pytest

from scripts.hive import launch, placement, registry
from scripts.hive.placement import Choice, choose
from tests.hive.test_controller_launch import NO_RAM, publish_room, write_hive
from tests.hive.test_controller_launch import setup as setup
from tests.hive.test_placement import NOW, hive

ORIGIN = "github.com/the-cloud-clockwork/agentihooks"
CHECKOUT = "/home/iamroot/dev/tcc-ecosystem/agentihooks"
POD = "/home/worker/dev/agentihooks"
ROOT_ACCOUNTS = (
    "nestor_coltatgmail_com",
    "nestor_coltatthecloudclockwork_com",
    "nestorcolt_smatgmail_com",
    "nestorcoltathotmail_com",
    "thecloudclockworkatgmail_com",
)
ANTON_ACCOUNT = "thecloudclockworkatgmail_com"
ANTON = "abd795cc73466951"


def root(**fields) -> dict:
    return hive(
        "root",
        **{
            "max_agents": 16,
            "slots": [
                {"harness": "claude", "account": name, "kind": "subscription", "cap": 6} for name in ROOT_ACCOUNTS
            ],
            "interactive": {"claude": "", "codex": ""},
            "repos": [CHECKOUT, ORIGIN],
            **fields,
        },
    )


def anton(**fields) -> dict:
    return hive(
        ANTON,
        **{
            "ui": "no",
            "ephemeral": "yes",
            "excluded": ["master"],
            "max_agents": 1,
            "slots": [{"harness": "claude", "account": ANTON_ACCOUNT, "kind": "subscription", "cap": 0}],
            "interactive": {"claude": "", "codex": ""},
            "repos": [POD, ORIGIN],
            **fields,
        },
    )


REFUSALS = {
    "an account only the workstation advertises": (
        "nestor_coltatgmail_com",
        anton(),
        Choice("root"),
    ),
    "a headless hive without the repo": (
        ANTON_ACCOUNT,
        anton(repos=[POD]),
        Choice("root"),
    ),
    "a full headless hive": (ANTON_ACCOUNT, anton(agents=1), Choice("root")),
    "an equal share prefers the durable workstation": (ANTON_ACCOUNT, anton(), Choice("root")),
}


@pytest.mark.parametrize(("account", "headless", "expected"), REFUSALS.values(), ids=REFUSALS.keys())
def test_workers_can_use_the_workstation_beside_a_headless_hive(account, headless, expected):
    assert choose("eng", "claude", account, "subscription", ORIGIN, [root(), headless], NOW) == expected


@pytest.fixture
def live(setup, tmp_path):
    from scripts.swarm.capacity import Account

    checkout = tmp_path / "agentihooks"
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".git" / "config").write_text(
        '[core]\n\tbare = false\n[remote "origin"]\n\turl = https://github.com/the-cloud-clockwork/agentihooks.git\n'
    )
    for record in (root(), anton()):
        write_hive(setup, **{**record, "heartbeat_at": setup.hive["heartbeat_at"]})
    setup.store.redis.delete(registry.key("remote"))
    setup.store.redis.srem(registry.INDEX, "remote")
    setup.store.redis.set(f"{registry.key('root')}:sessions", json.dumps({f"claude:{ROOT_ACCOUNTS[0]}": 1}))
    setup.herdr._quota_accounts = [Account("claude", name, "room", 0, 80, 80, 6) for name in ROOT_ACCOUNTS]
    setup.herdr.planned_account = lambda *args: ANTON_ACCOUNT
    setup.config = replace(setup.config, repo=str(checkout))
    return setup


def test_the_anton_hive_takes_an_engineer_for_a_swarm_repo_given_as_a_local_checkout(live):
    selected = live.launcher.select(live.config, "eng", {"id": "task"})
    assert isinstance(selected, launch.Selection)
    assert (selected.hive, selected.account.name, selected.fallback) == (ANTON, ANTON_ACCOUNT, "")


def test_an_engineer_placed_on_the_anton_hive_by_origin_launches_in_the_pod_checkout(live, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from scripts.hive.orders import Consumer
    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm.tick import Placed
    from scripts.swarm_v2.runtime.base import Outcome, Status
    from tests.hive.test_controller_launch import request_for

    pod = tmp_path / "pod" / "agentihooks"
    (pod / ".git").mkdir(parents=True)
    (pod / ".git" / "config").write_text(
        '[remote "origin"]\n\turl = https://github.com/the-cloud-clockwork/agentihooks.git\n'
    )
    monkeypatch.setenv("HIVE_REPO_ROOT", str(pod.parent))
    write_hive(live, **{**anton(repos=[str(pod), ORIGIN]), "heartbeat_at": live.hive["heartbeat_at"]})
    live.store.redis.hset(live.store.key("fixture", "config"), "repo", live.config.repo)
    launched, openings = [], []
    live.launcher.opening = lambda request: f"working in the repo {request.config.repo}"

    def spawn(request):
        openings.append(request.task["hive_opening"])
        home = tmp_path / "home"
        launched.append(
            HerdrRuntime(home=home)._argv(request.config, request.name, "claude", "open", "e.md", "engineer")
        )
        placed = Placed(
            "pane", "claude", ANTON_ACCOUNT, profile="engineer", profile_decision={"validation": {"pid": 12}}
        )
        return Outcome("spawn", Status.OK, "local", placed)

    runtime = Mock()
    runtime.spawn.side_effect = spawn
    runtime.namespace.return_value = "namespace"
    runtime.table.return_value = {12: SimpleNamespace(start_time=34)}
    consumer = Consumer(live.store.redis, ANTON, runtime)
    live.launcher.sleep = lambda _: consumer.poll()
    outcome = live.launcher.spawn(request_for(live))
    assert outcome.ok, outcome.detail
    assert outcome.value.execution.hive == ANTON
    argv = launched[0]
    assert argv[argv.index("--dir") + 1] == str(pod)
    assert openings == [f"working in the repo {ORIGIN}"]


def test_a_hive_that_also_lists_the_swarm_path_is_selected_with_no_origin(live):
    write_hive(live, **{**root(repos=[live.config.repo, ORIGIN]), "heartbeat_at": live.hive["heartbeat_at"]})
    anton_pick = live.launcher.select(live.config, "eng", {"id": "task"})
    root_pick = live.launcher.select(live.config, "eng", {"id": "task", "hive": "root"})
    assert (anton_pick.hive, anton_pick.origin) == (ANTON, ORIGIN)
    assert (root_pick.hive, root_pick.origin) == ("root", "")


def test_a_master_placed_by_origin_names_the_origin_to_launch_in(live):
    write_hive(live, **{**root(repos=[ORIGIN]), "heartbeat_at": live.hive["heartbeat_at"]})
    selected = live.launcher.select(live.config, "master", {"id": "master"})
    assert (selected.hive, selected.origin) == ("root", ORIGIN)


def test_a_full_anton_hive_sends_the_engineer_to_the_workstation_without_a_fallback(live):
    publish_room(live.store, ANTON, 0)
    selected = live.launcher.select(live.config, "eng", {"id": "task"})
    assert (selected.hive, selected.account.name) == ("root", ANTON_ACCOUNT)
    assert selected.fallback == ""


def test_a_hive_listing_another_origin_does_not_take_the_checkout(live):
    write_hive(
        live, **{**anton(repos=[POD, "github.com/someone/agentihooks"]), "heartbeat_at": live.hive["heartbeat_at"]}
    )
    selected = live.launcher.select(live.config, "eng", {"id": "task", "hive": ANTON})
    assert selected == placement.Wait(f"no hive for eng lists the repo {live.config.repo}")


def test_a_checkout_without_an_origin_matches_only_by_its_path(live, tmp_path):
    bare = tmp_path / "local"
    (bare / ".git").mkdir(parents=True)
    (bare / ".git" / "config").write_text("[core]\n\tbare = false\n")
    config = replace(live.config, repo=str(bare))
    assert live.launcher.select(config, "eng", {"id": "task", "hive": ANTON}) == placement.Wait(
        f"no hive for eng lists the repo {bare}"
    )
    write_hive(live, **{**anton(repos=[POD, ORIGIN, str(bare)]), "heartbeat_at": live.hive["heartbeat_at"]})
    assert live.launcher.select(config, "eng", {"id": "task", "hive": ANTON}).hive == ANTON


def test_a_tick_logs_the_wait_when_both_live_hives_are_full(live):
    from scripts.swarm.tick import tick
    from tests.swarm.test_tick import FakeRuntime, tasks

    publish_room(live.store, ANTON, 0)
    publish_room(live.store, "root", 0)

    class LiveHives(FakeRuntime):
        def placement_refusal(self, lane, task):
            selected = live.launcher.select(live.config, lane, task) if task.get("id") else None
            return selected.reason if isinstance(selected, placement.Wait) else ""

    runtime = LiveHives()
    live.store.create(replace(live.config, slug="sw"))
    actions = tick("sw", live.store, tasks(("t1", "eng")), runtime, now_ms=1_000)
    assert f"task t1 waits: no hive for eng has room: {ANTON} {NO_RAM}; root {NO_RAM}" in actions
    assert runtime.spawned == []


MISSING = placement.Wait(f"hive gone is not in the hive listing for {ORIGIN}")


def test_a_hive_missing_from_the_listing_is_refused_naming_the_origin(live):
    hives = launch._by_origin([root(repos=[live.config.repo, ORIGIN]), anton()], live.config.repo)
    assert launch._matched_origin(hives, "gone", live.config.repo) == MISSING
    assert launch._matched_origin(hives, ANTON, live.config.repo) == ORIGIN
    assert launch._matched_origin(hives, "root", live.config.repo) == ""


def test_an_engineer_choice_of_a_hive_missing_from_the_listing_waits_instead_of_raising(live, monkeypatch):
    monkeypatch.setattr(launch.placement, "choose", lambda *args: Choice("gone"))
    assert live.launcher.select(live.config, "eng", {"id": "task"}) == MISSING


def test_a_master_placed_on_a_hive_missing_from_the_listing_waits_instead_of_raising(live, monkeypatch):
    placed = placement.MasterPlacement("gone", ROOT_ACCOUNTS[0], "subscription", False)
    monkeypatch.setattr(launch.placement, "place_master", lambda *args: placed)
    assert live.launcher.select(live.config, "master", {"id": "master"}) == MISSING


def test_a_missing_hive_for_a_checkout_without_an_origin_is_refused_naming_its_path(tmp_path):
    bare = tmp_path / "local"
    (bare / ".git").mkdir(parents=True)
    (bare / ".git" / "config").write_text("[core]\n\tbare = false\n")
    assert launch._matched_origin([anton()], "gone", str(bare)) == placement.Wait(
        f"hive gone is not in the hive listing for {bare}"
    )
