from types import SimpleNamespace

import pytest

from scripts.hive import launch, placement, placement_waits, registry
from scripts.hive.auth import HiveError
from scripts.hive.placement import Wait
from scripts.swarm import cli, commands, controller, status
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2.runtime.base import LOCAL, Placement
from tests.hive.test_controller_launch import setup as setup
from tests.hive.test_controller_launch import write_hive
from tests.hive.test_local_overflow import hybrid
from tests.swarm.test_tick import FakeLedger

HEADED = {"id": "browse", "seat": "eng-1@fixture", "needs": ["headed"]}


def test_a_hive_declares_its_capabilities_and_starts_with_none():
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    assert registry._default("desk")["capabilities"] == []
    record = registry.update(redis, "desk", ["capabilities=headed, gpu,headed"])
    assert record["capabilities"] == ["headed", "gpu"]
    assert registry.show(redis, "desk")["capabilities"] == ["headed", "gpu"]
    assert registry.update(redis, "desk", ["capabilities="])["capabilities"] == []


def test_a_capability_name_must_be_a_plain_word():
    with pytest.raises(HiveError, match="^capabilities takes capability names, not 'head ed'$"):
        registry.parse(["capabilities=head ed"])


def test_a_normal_hive_skips_a_headed_task_with_a_waits_line(setup):
    assert isinstance(setup.launcher.select(setup.config, "eng", {"id": "other"}), launch.Selection)
    selected = setup.launcher.select(setup.config, "eng", HEADED)
    assert selected == Wait(placement.NEEDS_CAPABILITY.format(needs="headed"))
    assert selected.reason == "the task needs a hive with headed, and no hive declares it"
    assert selected.role == "eng"
    row = placement_waits.read(setup.store, setup.config.slug)["browse"]
    assert (row["lane"], row["reason"]) == ("eng", selected.reason)
    assert setup.launcher.select(setup.config, "eng", {**HEADED, "needs": ["headed", "gpu"]}).reason == (
        "the task needs a hive with headed, gpu, and no hive declares it"
    )


def test_a_headed_hive_claims_the_headed_task_over_a_preferred_normal_hive(setup):
    write_hive(setup, prefer={"eng": 1})
    write_hive(setup, id="desk", capabilities=["headed"], prefer={"eng": 9})
    assert setup.launcher.select(setup.config, "eng", {"id": "other"}).hive == "remote"
    assert setup.launcher.select(setup.config, "eng", HEADED).hive == "desk"
    assert setup.launcher.select(setup.config, "eng", {**HEADED, "needs": ["headed", "gpu"]}) == Wait(
        placement.NEEDS_CAPABILITY.format(needs="gpu")
    )
    write_hive(setup, capabilities=["gpu"])
    assert setup.launcher.select(setup.config, "eng", {**HEADED, "needs": ["headed", "gpu"]}) == Wait(
        "the task needs a hive with headed, gpu, and no single hive declares all of them"
    )


def test_a_hybrid_controller_never_overflows_a_headed_task_to_a_local_machine_without_it(setup, monkeypatch):
    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    fenced = hybrid(setup, monkeypatch)
    assert fenced.placement_refusal("eng", {"id": "other", "seat": "eng-2@fixture"}) == ""
    reason = placement.NEEDS_CAPABILITY.format(needs="headed")
    assert fenced.placement_refusal("eng", HEADED) == reason
    write_hive(setup, id="workstation", ui="yes", excluded=[], capabilities=["headed"], max_agents=8)
    assert fenced.placement_refusal("eng", HEADED) == ""


def test_a_hybrid_controller_overflows_a_headed_task_to_a_local_machine_that_declares_it(setup, monkeypatch):
    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    fenced = hybrid(setup, monkeypatch)
    write_hive(setup, id="workstation", ui="yes", excluded=[], capabilities=["headed"], max_agents=8, heartbeat_at=0)
    assert fenced.placement_refusal("eng", HEADED) == ""
    assert fenced.spawn(setup.config, "eng", "eng name", HEADED) == "placed on the workstation"
    fenced.runtime.hive_launch.spawn.assert_not_called()


def test_a_local_swarm_refuses_a_headed_task_unless_the_local_hive_declares_it(setup, monkeypatch):
    monkeypatch.setattr(commands, "hive_id", lambda: "workstation")
    fenced = controller.FencedRuntime(setup.store, setup.config.slug, setup.held, setup.herdr, "local")
    assert fenced.placement_refusal("eng", HEADED) == placement.NEEDS_CAPABILITY.format(needs="headed")
    write_hive(setup, id="workstation", ui="yes", excluded=[], capabilities=["headed"], max_agents=8)
    assert fenced.placement_refusal("eng", HEADED) == ""


def test_runtime_placement_keeps_a_task_with_needs_off_backends_without_capabilities():
    assert Placement("kubernetes").backend_for("eng", HEADED) == LOCAL
    assert Placement("kubernetes").backend_for("eng", {**HEADED, "needs": []}) == "kubernetes"
    assert Placement("hive").backend_for("eng", HEADED) == "hive"


def test_swarm_status_prints_each_agents_hive(monkeypatch, capsys):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/r", 4, 0))
    names = {
        hive: store.start_execution(
            "sw", AgentRecord(store.next_name("sw", "eng", slot), "eng", f"t{slot}", seat=f"eng-{slot}@sw", hive=hive)
        ).name
        for slot, hive in enumerate(("desk", ""), start=1)
    }
    monkeypatch.setattr(cli, "LedgerClient", lambda: FakeLedger([]))
    monkeypatch.setattr(status.HerdrRuntime, "live_names", lambda self: set(names.values()))
    monkeypatch.setattr(commands, "hive_id", lambda: "")
    cli.cmd_status(store, SimpleNamespace(slug="sw", json=False))
    lines = capsys.readouterr().out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Agent\t"))
    head, *rows = [line.split("\t") for line in lines[start : start + 3]]
    column = head.index("Hive")
    assert {row[0]: row[column] for row in rows} == {names["desk"]: "desk", names[""]: "-"}
