from typing import Any, cast

import pytest

from scripts.swarm_v2.kubernetes import replacement
from tests import sv2_kub05_cases as cases
from tests.test_swarm_v2_kubernetes_failures import _Ledger, _Relaunch
from tests.test_swarm_v2_kubernetes_failures import world as world

pytestmark = pytest.mark.unit


def test_a_node_lost_retry_reopens_for_hive_placement_without_relaunching(world: Any) -> None:
    recovery, ledger, relaunch = world.recovery(world.live), _Ledger(), _Relaunch()
    relaunch.recovery = recovery
    replacements = replacement.Replacements(recovery, cast(Any, relaunch), ledger, placed=True)
    world.show(world.old, "node_lost")
    world.reconcile(recovery)

    assert replacements() == {world.old.execution_id: "placed"}
    assert relaunch.calls == []
    assert ledger.rows[0]["state"] == "open"
    assert ledger.rows[0]["claimed_by"] == ""
    successor = recovery.decision(world.old.execution_id).replacement
    assert world.store.execution(cases.SLUG, successor).state == "ended"
    assert replacements() == {}


@pytest.mark.parametrize("state,claimant", [("claimed", "another-worker"), ("done", ""), ("pr", "another-worker")])
def test_an_unschedulable_attempt_never_reopens_reassigned_or_finished_work(
    world: Any, state: str, claimant: str
) -> None:
    from scripts.swarm_v2.kubernetes.unschedulable import Unschedulable
    from tests.test_swarm_v2_kubernetes_failures import _unschedulable

    row = {"id": "task", "state": state, "claimed_by": claimant}
    ledger = _Ledger(rows=[dict(row)])
    ending = Unschedulable(world.store, cases.SLUG, world.live, ledger, window_ms=1)
    _unschedulable(world, world.old)
    pods = list(world.api.objects.values())
    ending(pods)
    world.clock[0] += 1

    assert ending(pods) == {world.old.execution_id: "ended"}
    assert ledger.rows == [row]
    assert ledger.calls == []


def test_unschedulable_finalization_resumes_after_the_agent_was_ended(
    world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm_v2 import retention
    from scripts.swarm_v2.kubernetes.unschedulable import Unschedulable
    from tests.test_swarm_v2_kubernetes_failures import _unschedulable

    ledger = _Ledger()
    ending = Unschedulable(world.store, cases.SLUG, world.live, ledger, window_ms=1)
    _unschedulable(world, world.old)
    pods = list(world.api.objects.values())
    ending(pods)
    world.clock[0] += 1
    finalize = retention.finalize

    def interrupted(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("interrupted finalization")

    monkeypatch.setattr(retention, "finalize", interrupted)
    with pytest.raises(ConnectionError, match="interrupted finalization"):
        ending(pods)
    assert world.store.execution(cases.SLUG, world.old.execution_id).state == "ended"
    monkeypatch.setattr(retention, "finalize", finalize)

    assert ending(pods) == {world.old.execution_id: "ended"}
    final = retention.final(world.store, cases.SLUG, world.old.execution_id)
    assert final is not None
    assert final.state == "cancelled"
