"""Hive placement counts no master session as a worker on any login route: a master on the claude ai or master login
route leaves through the hive's interactive subtotal, a master the hive lists on the subscription or api route leaves
through its listed execution, and real workers, stale master records and the host CPU line still bind."""

import json
from typing import Any

import pytest

from scripts.hive import headroom, launch, registry
from scripts.swarm import commands
from scripts.swarm.profile_choice import ProfileDecision
from scripts.swarm.store import AgentRecord
from tests.hive.test_controller_launch import setup, write_hive

pytestmark = pytest.mark.unit
_fixtures = (setup,)


def _record(setup: Any, lane: str, hive: str, route: str = "subscription", account: str = "account") -> AgentRecord:
    slug = setup.config.slug
    name = setup.store.next_name(slug, lane)
    index = len(setup.store.agents(slug)) + 1
    return setup.store.start_execution(
        slug,
        AgentRecord(
            name,
            lane,
            lane if lane == "master" else "task",
            seat=f"{lane}-{index}@{slug}",
            harness="claude",
            account=account,
            hive=hive,
            route_kind=route,
            runtime_target={"host_id": hive},
        ),
    )


def _report(setup: Any, hive: str, report: dict[str, Any]) -> None:
    setup.store.redis.set(f"{registry.key(hive)}:sessions", json.dumps(report))


def _listed(sessions: int, *records: AgentRecord) -> dict[str, Any]:
    return {"claude:account": sessions, "_executions": {"claude:account": [record.execution_id for record in records]}}


def _loaded(setup: Any, hive: str) -> dict[str, Any]:
    return next(found for found in launch.loaded_hives(setup.store) if found["id"] == hive)


@pytest.fixture
def remote(setup: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    write_hive(setup, max_agents=2)
    return setup


@pytest.mark.feature("hive-worker-total-excludes-masters-by-role")
@pytest.mark.parametrize(
    ("route", "marked"),
    [("interactive", 2), ("master-login", 2), ("subscription", 0), ("api", 0)],
    ids=["claude-ai-login", "master-login", "subscription", "api"],
)
def test_a_master_on_any_login_route_is_no_worker_and_the_real_worker_still_counts(
    remote: Any, route: str, marked: int
) -> None:
    write_hive(remote, max_agents=3)
    worker = _record(remote, "eng", "remote")
    masters = [_record(remote, "master", "remote", route) for _ in range(2)]
    report = _listed(4, worker, *masters)
    if marked:
        report["interactive:claude:account"] = marked
    _report(remote, "remote", report)
    hive = _loaded(remote, "remote")
    assert (hive["agents"], hive["room"], hive["refusal"]) == (2, 1, "")


@pytest.mark.feature("hive-worker-total-excludes-masters-by-role")
def test_masters_listed_under_both_spellings_of_one_account_all_leave_its_worker_total(remote: Any) -> None:
    write_hive(remote, max_agents=3)
    masters = [_record(remote, "master", "remote", account=account) for account in ("my_acct", "my-acct")]
    executions = {"claude:my_acct": [masters[0].execution_id], "claude:my-acct": [masters[1].execution_id]}
    _report(remote, "remote", {"claude:my_acct": 3, "_executions": executions})
    assert _loaded(remote, "remote")["agents"] == 1


@pytest.mark.feature("hive-worker-total-excludes-masters-by-role")
@pytest.mark.parametrize(
    "report",
    [
        {"claude:account": 1, "interactive:claude:account": 1, "claude:other": 1},
        {"claude:account": 1, "claude:other": 1},
    ],
    ids=["slot-all-marked", "slot-unmarked"],
)
def test_a_listed_master_never_removes_more_than_its_slot_holds_without_the_marker(
    remote: Any, report: dict[str, int]
) -> None:
    master = _record(remote, "master", "remote")
    _report(remote, "remote", {**report, "_executions": {"claude:account": [master.execution_id]}})
    assert _loaded(remote, "remote")["agents"] == 1


@pytest.mark.feature("hive-worker-total-excludes-masters-by-role")
def test_a_root_master_with_no_hive_name_leaves_only_the_root_worker_total(remote: Any) -> None:
    write_hive(remote, id="root", max_agents=2)
    worker = _record(remote, "eng", "root")
    master = _record(remote, "master", "")
    _report(remote, "root", _listed(2, worker, master))
    _report(remote, "remote", _listed(1))
    assert (_loaded(remote, "root")["agents"], _loaded(remote, "remote")["agents"]) == (1, 1)


@pytest.mark.feature("hive-worker-total-excludes-masters-by-role")
def test_a_master_record_the_hive_does_not_list_removes_no_real_worker(remote: Any) -> None:
    _record(remote, "master", "remote")
    gone = _record(remote, "master", "remote", account="gone")
    _report(remote, "remote", {"claude:account": 2, "_executions": {"claude:gone": [gone.execution_id]}})
    assert _loaded(remote, "remote")["agents"] == 2


@pytest.mark.feature("hive-worker-total-excludes-masters-by-role")
def test_zero_cpu_room_still_refuses_once_the_master_leaves_the_worker_total(remote: Any) -> None:
    worker = _record(remote, "eng", "remote")
    master = _record(remote, "master", "remote")
    _report(remote, "remote", _listed(2, worker, master))
    reading = json.loads(remote.store.redis.get(headroom.key("remote")))
    remote.store.redis.set(headroom.key("remote"), json.dumps({**reading, "cpus": 16, "load5": 15.0}))
    hive = _loaded(remote, "remote")
    assert (hive["agents"], hive["room"]) == (1, 0)
    assert hive["refusal"].startswith("cpu: ")


@pytest.mark.feature("hive-worker-total-excludes-masters-by-role")
def test_an_expert_takes_the_root_hive_whose_count_only_a_subscription_master_fills(remote: Any) -> None:
    remote.herdr._profile_decision.return_value = ProfileDecision("expert", "task", "expert")
    write_hive(remote, excluded=["master", "expert"])
    write_hive(remote, id="root", ui="no", excluded=["master"], max_agents=2)
    worker = _record(remote, "eng", "root")
    master = _record(remote, "master", "root")
    _report(remote, "root", _listed(2, worker, master))
    selected = remote.launcher.select(remote.config, "expert", {"id": "expert-1", "requires_harness": "claude"})
    assert isinstance(selected, launch.Selection)
    assert selected.hive == "root"
