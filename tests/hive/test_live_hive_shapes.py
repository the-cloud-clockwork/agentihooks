from dataclasses import replace

import pytest

from scripts.hive import launch, placement, registry
from scripts.hive.placement import Choice, choose
from tests.hive.test_controller_launch import setup as setup
from tests.hive.test_controller_launch import write_hive
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
            "roles": ["master", "eng", "ci", "plan", "dispatch"],
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
            "roles": ["eng"],
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
        Choice("root", "no headless hive for eng advertises the claude slot nestor_coltatgmail_com"),
    ),
    "a headless hive without the repo": (
        ANTON_ACCOUNT,
        anton(repos=[POD]),
        Choice("root", f"no headless hive for eng lists the repo {ORIGIN}"),
    ),
    "a full headless hive": (ANTON_ACCOUNT, anton(agents=1), Choice("root", "no headless hive for eng has room")),
    "the headless hive takes its own account": (ANTON_ACCOUNT, anton(), Choice(ANTON)),
}


@pytest.mark.parametrize(("account", "headless", "expected"), REFUSALS.values(), ids=REFUSALS.keys())
def test_a_headless_refusal_for_any_reason_falls_back_to_the_workstation(account, headless, expected):
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
    setup.herdr._quota_accounts = [Account("claude", name, "room", 0, 80, 80, 6) for name in ROOT_ACCOUNTS]
    setup.config = replace(setup.config, repo=str(checkout))
    return setup


def test_the_anton_hive_takes_an_engineer_for_a_swarm_repo_given_as_a_local_checkout(live):
    selected = live.launcher.select(live.config, "eng", {"id": "task"})
    assert isinstance(selected, launch.Selection)
    assert (selected.hive, selected.account.name, selected.fallback) == (ANTON, ANTON_ACCOUNT, "")


def test_a_full_anton_hive_sends_the_engineer_to_the_workstation_with_the_reason(live):
    live.store.redis.set(f"{registry.key(ANTON)}:sessions", '{"eng": 1}')
    selected = live.launcher.select(live.config, "eng", {"id": "task"})
    assert (selected.hive, selected.account.name) == ("root", ROOT_ACCOUNTS[0])
    assert selected.fallback == "no headless hive for eng has room"


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

    live.store.redis.set(f"{registry.key(ANTON)}:sessions", '{"eng": 1}')
    live.store.redis.set(f"{registry.key('root')}:sessions", '{"eng": 16}')

    class LiveHives(FakeRuntime):
        def placement_refusal(self, lane, task):
            selected = live.launcher.select(live.config, lane, task) if task.get("id") else None
            return selected.reason if isinstance(selected, placement.Wait) else ""

    runtime = LiveHives()
    live.store.create(replace(live.config, slug="sw"))
    actions = tick("sw", live.store, tasks(("t1", "eng")), runtime, now_ms=1_000)
    assert "task t1 waits: no headless hive for eng has room" in actions
    assert runtime.spawned == []
