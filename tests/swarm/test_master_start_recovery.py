from unittest.mock import patch

import pytest

from scripts.swarm import master_start
from scripts.swarm.store import MASTER
from scripts.swarm.tick import tick
from tests.swarm.test_master_start import StartupRuntime
from tests.swarm.test_tick import store, tasks  # noqa: F401

pytestmark = pytest.mark.unit


def test_alerted_master_relaunches_with_original_handoff_within_five_minutes(store):
    runtime, ledger = StartupRuntime(), tasks()
    runtime.crash = RuntimeError("no master claude ai login")
    store.put_handoff("sw", MASTER, "Original complete handoff")
    tick("sw", store, ledger, runtime, 1)
    tick("sw", store, ledger, runtime, 2)
    tick("sw", store, ledger, runtime, 120_002)
    pending = master_start.read(store, "sw")
    assert pending["alerted"] is True
    original = pending["task"]
    runtime.crash = None
    with patch.object(runtime, "spawn", wraps=runtime.spawn) as spawn:
        tick("sw", store, ledger, runtime, 420_001)
        assert spawn.call_count == 0
        tick("sw", store, ledger, runtime, 420_002)
    assert len(runtime.masters) == 1, "master seat remains empty 5 minutes after login recovery"
    name, task = runtime.masters[0]
    assert task["handoff"] == original["handoff"]
    assert task["transfer"] == original["transfer"]
    assert store.handoff("sw", MASTER) == "Original complete handoff"
    runtime.registered.add(name)
    tick("sw", store, ledger, runtime, 420_003)
    assert master_start.read(store, "sw") == {}
    assert store.handoff("sw", MASTER) == ""
