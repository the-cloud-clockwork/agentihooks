from types import SimpleNamespace

import pytest

from scripts.swarm.runtime import HerdrRuntime
from tests.swarm.profile_fixture import validated


def test_hive_runtime_reserves_and_pins_account_before_start(tmp_path):
    calls = []

    def run(argv, **kwargs):
        assert argv[:3] == ["env", "AGENTIHOOKS_SLOT_HIVE=hive-a", "AGENTIHOOKS_SLOT_EXECUTION=execution-a"]
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "open"))
    config = SimpleNamespace(
        slug="rig", repo=str(tmp_path), code="a1b2c3", compact_limit=0, lanes={}, autonomy="delegate"
    )
    runtime.spawn(
        config,
        "eng",
        "agent-a",
        {"id": "one", "title": "work", "account_slot_hive": "hive-a", "execution_id": "execution-a"},
    )
    assert len(calls) == 1


def test_hive_runtime_account_refusal_does_not_launch(tmp_path, monkeypatch):
    from scripts.swarm.tick import SpawnError

    def refused(argv, **kwargs):
        assert argv[:3] == ["env", "AGENTIHOOKS_SLOT_HIVE=hive-a", "AGENTIHOOKS_SLOT_EXECUTION=execution-a"]
        return SimpleNamespace(returncode=3, stdout="", stderr="No account slot available")

    runtime = HerdrRuntime(home=tmp_path, run=refused, choose=lambda *_: ("claude", "open"))
    terminated = []
    monkeypatch.setattr(runtime, "_terminate", terminated.append)
    config = SimpleNamespace(
        slug="rig", repo=str(tmp_path), code="a1b2c3", compact_limit=0, lanes={}, autonomy="delegate"
    )
    with pytest.raises(SpawnError, match="^No account slot available$"):
        runtime.spawn(
            config,
            "eng",
            "agent-a",
            {"id": "one", "title": "work", "account_slot_hive": "hive-a", "execution_id": "execution-a"},
        )
    assert terminated == ["agent-a"]
