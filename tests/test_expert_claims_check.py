"""The live check that an expert claims nothing, over a stand-in store and ledger."""

from types import SimpleNamespace
from typing import Any

import pytest

from scripts.feature_checks import expert_claims as check
from scripts.swarm.store import SwarmError

pytestmark = pytest.mark.unit

EXPERT, OTHER_EXPERT, ENGINEER = "expert@a1b2c3-0001", "expert@a1b2c3-0002", "engineer@a1b2c3-0001"


class Store:
    def __init__(self, swarms: tuple[str, ...], stopped: tuple[str, ...] = ()) -> None:
        self.swarms, self.stopped = swarms, stopped

    def slugs(self) -> list[str]:
        return list(self.swarms)

    def config(self, slug: str) -> SimpleNamespace:
        return SimpleNamespace(state="stopped" if slug in self.stopped else "running")


def _row(task: str, claimed_by: str | None = "") -> dict[str, Any]:
    return {"id": task, "claimed_by": claimed_by, "state": "claimed" if claimed_by else "open"}


def _ledger(rows: dict[str, list[dict[str, Any]]]) -> check.Tasks:
    def tasks(slug: str) -> list[dict[str, Any]]:
        if slug not in rows:
            raise SwarmError(f"ledger {slug} is gone")
        return rows[slug]

    return tasks


def test_a_fleet_with_no_running_swarm_passes_and_says_so(capsys: pytest.CaptureFixture[str]) -> None:
    assert check.main(Store(("old",), stopped=("old",)), _ledger({})) == 0
    assert capsys.readouterr().out == "no swarm is running\n"


@pytest.mark.feature("expert-claims-nothing")
def test_each_swarm_whose_tasks_no_expert_claims_passes_and_is_listed(capsys: pytest.CaptureFixture[str]) -> None:
    rows = {
        "rig": [_row("t1", ENGINEER), _row("t2"), _row("t3", None), {"id": "t4"}],
        "doctor": [],
        "old": [_row("t9", EXPERT)],
    }
    assert check.main(Store(("rig", "old", "doctor"), stopped=("old",)), _ledger(rows)) == 0
    assert capsys.readouterr().out.splitlines() == [
        "doctor holds 0 tasks and none is claimed by an expert",
        "rig holds 4 tasks and none is claimed by an expert",
    ]


@pytest.mark.feature("expert-claims-nothing")
def test_a_task_claimed_by_an_expert_fails_by_name(capsys: pytest.CaptureFixture[str]) -> None:
    rows = {
        "rig": [_row("t1", ENGINEER), _row("t2", EXPERT), _row("t3", OTHER_EXPERT)],
        "doctor": [_row("t1", ENGINEER)],
    }
    assert check.main(Store(("rig", "doctor")), _ledger(rows)) == 1
    assert capsys.readouterr().out.splitlines() == [
        "doctor holds 1 tasks and none is claimed by an expert",
        f"FAIL: rig task t2 is claimed by the expert {EXPERT}",
        f"FAIL: rig task t3 is claimed by the expert {OTHER_EXPERT}",
    ]


def test_a_swarm_whose_tasks_cannot_be_read_fails_by_name(capsys: pytest.CaptureFixture[str]) -> None:
    assert check.main(Store(("rig", "lost")), _ledger({"rig": []})) == 1
    assert capsys.readouterr().out.splitlines() == [
        "FAIL: lost: its tasks could not be read: ledger lost is gone",
        "rig holds 0 tasks and none is claimed by an expert",
    ]


def test_with_no_store_given_the_check_reads_the_swarm_store_and_the_ledger(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.swarm import ledger_client
    from scripts.swarm import store as swarm_store

    client = object()
    found = Store(("rig",))
    monkeypatch.setattr(swarm_store, "redis_client", lambda: client)
    monkeypatch.setattr(swarm_store, "RedisStore", lambda redis: found if redis is client else None)
    monkeypatch.setattr(
        ledger_client, "LedgerClient", lambda: SimpleNamespace(tasks=_ledger({"rig": [_row("t1", EXPERT)]}))
    )
    assert check.main() == 1
    assert capsys.readouterr().out == f"FAIL: rig task t1 is claimed by the expert {EXPERT}\n"
