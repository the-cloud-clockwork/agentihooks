from dataclasses import replace
from typing import Any

import pytest

from scripts import claude_quota_balancer as balancer
from scripts import master_logins
from scripts.hive import daemon, launch, placement, registry
from scripts.swarm.capacity import Account
from tests.hive.test_controller_launch import setup as setup
from tests.hive.test_controller_launch import write_hive
from tests.hive.test_daemon import no_presence as no_presence
from tests.hive.test_daemon import redis as redis
from tests.hive.test_daemon import timer_state as timer_state
from tests.hive.test_master_fallback_launch import master, master_login_hive
from tests.hive.test_master_placement import NOW, REPO, hive

pytestmark = pytest.mark.unit


def advertised() -> list[dict[str, Any]]:
    return [
        {"harness": "claude", "account": "login", "slot": 1, "week_left": 0.0},
        {"harness": "claude", "account": "spare", "slot": 4, "week_left": 95.0},
    ]


@pytest.mark.feature("hive-all-master-logins")
def test_beat_advertises_every_stored_login_and_the_best_legacy_account(
    redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        daemon, "observe", lambda **kwargs: {"slots": [], "interactive": {}, "repos": [], "sessions": {}, "quota": {}}
    )
    monkeypatch.setattr(daemon.master_claude_login, "signed_in", lambda: "login")
    monkeypatch.setattr(daemon.master_claude_login, "unusable", lambda: "")
    monkeypatch.setattr(daemon.master_claude_login, "logins", lambda: {1: "login", 4: "spare"})
    monkeypatch.setattr(daemon.master_claude_login, "failed", lambda number: False)
    monkeypatch.setattr(daemon.master_claude_login, "preferred", lambda: {})
    monkeypatch.setattr(daemon.master_claude_login, "slot", lambda: 1)
    monkeypatch.setattr(
        master_logins, "usage", lambda account, now: (0.0, {"login": 100.0, "spare": 5.0}[account], now)
    )
    daemon.beat(redis, "machine")
    record = registry.show(redis, "machine")
    assert record is not None
    assert record["master_login"] == {"claude": "spare"}
    assert record["master_logins"] == [advertised()[1], advertised()[0]]
    assert record["master_login_enabled"] == "yes"


@pytest.mark.feature("hive-all-master-logins")
@pytest.mark.parametrize(
    ("readings", "failed", "preferred", "marker", "expected"),
    [
        ({"login": (0, 50), "spare": (0, 5)}, set(), {}, 1, "spare"),
        ({"login": (0, 50), "spare": (0, 5)}, {4}, {}, 1, "login"),
        ({"login": (0, 98), "spare": (0, 97)}, set(), {"login": 0}, 1, "spare"),
        ({"login": (0, 50), "spare": (0, 5)}, set(), {"login": 0}, 1, "login"),
        ({"login": (99, 0), "spare": (0, 20)}, set(), {}, 1, "spare"),
        ({"login": (0, 5), "spare": (0, 5)}, set(), {"other": 0}, 4, "spare"),
        ({"login": (0, 5), "spare": (0, 5)}, set(), {}, 3, "login"),
    ],
)
def test_best_legacy_login_follows_the_launch_ranking(
    redis: Any,
    monkeypatch: pytest.MonkeyPatch,
    readings: dict[str, tuple[float, float]],
    failed: set[int],
    preferred: dict[str, int],
    marker: int,
    expected: str,
) -> None:
    monkeypatch.setattr(
        daemon, "observe", lambda **kwargs: {"slots": [], "interactive": {}, "repos": [], "sessions": {}, "quota": {}}
    )
    monkeypatch.setattr(daemon.master_claude_login, "signed_in", lambda: "login")
    monkeypatch.setattr(daemon.master_claude_login, "unusable", lambda: "")
    monkeypatch.setattr(daemon.master_claude_login, "logins", lambda: {4: "spare", 1: "login"})
    monkeypatch.setattr(daemon.master_claude_login, "failed", lambda number: number in failed)
    monkeypatch.setattr(daemon.master_claude_login, "preferred", lambda: preferred)
    monkeypatch.setattr(daemon.master_claude_login, "slot", lambda: marker)
    monkeypatch.setattr(master_logins, "usage", lambda account, now: (*readings[account], now))
    daemon.beat(redis, "machine")
    record = registry.show(redis, "machine")
    assert record is not None
    assert record["master_login"] == {"claude": expected}


@pytest.mark.feature("hive-all-master-logins")
@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_advertised_private_login_passes_hive_checks_without_a_worker_slot(harness: str) -> None:
    record = hive(
        "private",
        slots=[],
        master_login={},
        master_logins=[
            {"harness": "other", "account": "foreign", "slot": 1, "week_left": 100},
            {"harness": harness, "account": "spare", "slot": 4, "week_left": 95},
        ],
    )
    assert placement.place_master(harness, "", REPO, [record], NOW, front=True) == placement.MasterPlacement(
        "private", "spare", "master-login", False
    )
    assert placement.choose("master", harness, "spare", "master-login", REPO, [record], NOW) == placement.Choice(
        "private"
    )
    assert placement.choose("master", harness, "foreign", "master-login", REPO, [record], NOW) == placement.Wait(
        f"no hive for master holds the {harness} master login for foreign"
    )


@pytest.mark.parametrize("login", ["", "spare"])
@pytest.mark.feature("hive-all-master-logins")
def test_front_door_tries_slot_four_when_marker_slot_one_is_closed(login: str) -> None:
    record = hive("private", master_login={"claude": "login"}, master_logins=advertised())
    assert placement.place_master(
        "claude", login, REPO, [record], NOW, front=True, closed=frozenset({"login"})
    ) == placement.MasterPlacement("private", "spare", "master-login", False)


@pytest.mark.feature("hive-all-master-logins")
def test_legacy_record_and_secondary_master_keep_their_routes() -> None:
    record = hive("private", master_login={"claude": "login"})
    record.pop("master_logins", None)
    assert placement.place_master("claude", "", REPO, [record], NOW, front=True) == placement.MasterPlacement(
        "private", "login", "master-login", False
    )
    record["master_logins"] = advertised()
    assert placement.place_master("claude", "", REPO, [record], NOW) == placement.MasterPlacement(
        "private", "token", "subscription", False
    )


@pytest.mark.feature("hive-all-master-logins")
def test_front_door_keeps_advertised_ranking_instead_of_account_alphabetical_order() -> None:
    record = hive("private", master_login={"claude": "spare"}, master_logins=list(reversed(advertised())))
    assert placement.place_master("claude", "", REPO, [record], NOW, front=True) == placement.MasterPlacement(
        "private", "spare", "master-login", False
    )


@pytest.mark.parametrize("status", ["closed", "revoked", "rejected"])
@pytest.mark.feature("hive-all-master-logins")
def test_launch_uses_an_open_sibling_of_the_closed_or_rejected_marker(
    setup: Any, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    task = {**master(setup), "seat": "master@fixture"}
    master_login_hive(setup)
    write_hive(setup, master_logins=advertised())
    setup.herdr._quota_accounts = [
        Account("claude", "login", "CLOSED", 0, 80, 0, 0),
        Account("claude", "spare", "OPEN", 0, 80, 95, 6),
    ]
    observed = replace(balancer.revoked_result("login", 100), provider_status=status)
    monkeypatch.setattr(balancer, "cached_observations", lambda **_: [(100, observed)])
    selected = setup.launcher.select(setup.config, "master", task)
    assert isinstance(selected, launch.Selection)
    assert (selected.hive, selected.account.name, selected.route_kind) == ("remote", "spare", "master-login")


@pytest.mark.feature("hive-all-master-logins")
def test_all_advertised_logins_closed_keep_each_master_floor_reason(setup: Any) -> None:
    task = {**master(setup), "seat": "master@fixture"}
    master_login_hive(setup)
    write_hive(setup, master_logins=advertised())
    setup.herdr._quota_accounts = [
        Account("claude", "login", "CLOSED", 0, 80, 0, 0),
        Account("claude", "spare", "CLOSED", 0, 80, 1, 0),
    ]
    assert setup.launcher.select(setup.config, "master", task) == placement.Wait(
        "no hive for master advertises a claude slot; login is at the master floor with 0% of its week left; "
        "spare is at the master floor with 1% of its week left"
    )
