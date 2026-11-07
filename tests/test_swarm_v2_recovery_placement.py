from typing import Any

import pytest

from scripts.swarm_v2.kubernetes import replacement
from tests import sv2_kub05_cases as cases
from tests.test_swarm_v2_kubernetes_failures import _Ledger, _Relaunch
from tests.test_swarm_v2_kubernetes_failures import world as world

pytestmark = pytest.mark.unit


def test_a_node_lost_retry_reopens_for_hive_placement_without_relaunching(world: Any) -> None:
    recovery, ledger, relaunch = world.recovery(world.live), _Ledger(), _Relaunch()
    relaunch.recovery = recovery
    replacements = replacement.Replacements(recovery, relaunch, ledger, placed=True)
    world.show(world.old, "node_lost")
    world.reconcile(recovery)

    assert replacements() == {world.old.execution_id: "placed"}
    assert relaunch.calls == []
    assert ledger.rows[0]["state"] == "open"
    assert ledger.rows[0]["claimed_by"] == ""
    successor = recovery.decision(world.old.execution_id).replacement
    assert world.store.execution(cases.SLUG, successor).state == "ended"
    assert replacements() == {}
