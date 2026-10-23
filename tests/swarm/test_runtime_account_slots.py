from types import SimpleNamespace

import pytest

from scripts.swarm.runtime import HerdrRuntime
from tests.swarm.profile_fixture import validated


def test_hive_runtime_reserves_and_pins_account_before_start(tmp_path, monkeypatch):
    from scripts.hive import account_slots

    calls = []

    def admitted(hive, execution, argv, invoke):
        calls.append((hive, execution))
        return invoke([*argv[: argv.index("--") + 1], "--route", "a", *argv[argv.index("--") + 1 :]])

    monkeypatch.setattr(account_slots, "launch", admitted)

    def run(argv, **kwargs):
        assert calls == [("hive-a", "execution-a")]
        assert argv[argv.index("--") + 1 :][:2] == ["--route", "a"]
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(home=tmp_path, run=run, choose=lambda *_: ("claude", "open"))
    config = SimpleNamespace(
        slug="rig", repo=str(tmp_path), code="a1b2c3", compact_limit=0, lanes={}, autonomy="delegate"
    )
    runtime.spawn(config, "eng", "agent-a", {"id": "one", "account_slot_hive": "hive-a", "execution_id": "execution-a"})


def test_hive_runtime_account_refusal_does_not_launch(tmp_path, monkeypatch):
    from scripts.hive import account_slots
    from scripts.swarm.tick import SpawnError

    def refused(*args):
        raise account_slots.Unavailable("No account slot available")

    monkeypatch.setattr(account_slots, "launch", refused)
    calls = []
    runtime = HerdrRuntime(home=tmp_path, run=lambda *a, **kw: calls.append(a), choose=lambda *_: ("claude", "open"))
    config = SimpleNamespace(
        slug="rig", repo=str(tmp_path), code="a1b2c3", compact_limit=0, lanes={}, autonomy="delegate"
    )
    with pytest.raises(SpawnError, match="^No account slot available$"):
        runtime.spawn(
            config, "eng", "agent-a", {"id": "one", "account_slot_hive": "hive-a", "execution_id": "execution-a"}
        )
    assert calls == []
