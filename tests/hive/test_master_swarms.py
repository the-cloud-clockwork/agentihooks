import pytest

from scripts.hive import placement, registry
from scripts.hive.auth import HiveError
from tests.hive.test_controller_launch import setup as setup
from tests.hive.test_controller_launch import write_hive
from tests.hive.test_master_placement import NOW, REPO, hive

SCRATCH = "proof-323133-t1199-1"
LIVE = "rig-grade-swarm"


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


def headless(name="headless", **fields):
    return hive(name, ui="no", excluded=["master"], master_swarms=[SCRATCH], **fields)


def test_a_hive_admits_masters_only_for_the_swarms_it_names():
    hives = [headless()]

    assert placement.place_master("claude", "", REPO, placement.for_swarm(hives, SCRATCH), NOW) == (
        placement.MasterPlacement("headless", "token", "subscription", False)
    )
    assert placement.place_master("claude", "", REPO, placement.for_swarm(hives, LIVE), NOW) == placement.Wait(
        "no live hive takes master"
    )
    assert hives[0]["excluded"] == ["master"]


def test_naming_a_master_swarm_admits_no_other_excluded_role():
    (admitted,) = placement.for_swarm([headless(excluded=["master", "qa"])], SCRATCH)

    assert admitted["excluded"] == ["qa"]
    assert placement.hosts("master", admitted) and not placement.hosts("qa", admitted)


def test_a_hive_without_master_swarms_keeps_its_exclusions_for_every_swarm():
    record = hive("plain", ui="no", excluded=["master"])

    assert placement.for_swarm([record], SCRATCH) == [record]


def test_master_swarms_is_a_hive_setting_kept_off_the_default_record(redis):
    assert "master_swarms" not in registry.update(redis, "box", ["max-agents=3"])
    record = registry.update(redis, "box", [f"master-swarms={SCRATCH},{SCRATCH}, other-proof"])

    assert record["master_swarms"] == [SCRATCH, "other-proof"]
    assert registry.show(redis, "box")["master_swarms"] == [SCRATCH, "other-proof"]
    assert registry.update(redis, "box", ["master-swarms="])["master_swarms"] == []


def test_a_kept_registration_still_takes_the_charted_master_swarms(redis):
    registry.update(redis, "box", ["master-swarms=old"])

    assert registry.update(redis, "box", [f"master-swarms={SCRATCH}"], keep=True)["master_swarms"] == [SCRATCH]


def test_a_malformed_master_swarm_name_is_refused(redis):
    with pytest.raises(HiveError) as refused:
        registry.update(redis, "box", ["master-swarms=good,bad name"])

    assert str(refused.value) == "master-swarms takes swarm slugs, not 'bad name'"
    assert registry.show(redis, "box") is None


def test_hive_launch_places_a_master_of_a_named_swarm_on_a_headless_hive(setup):
    from scripts.hive.launch import Selection
    from scripts.swarm.profile_choice import ProfileDecision

    setup.herdr._profile_decision.return_value = ProfileDecision("master", "lane", "master")
    write_hive(setup, excluded=["master"], master_swarms=[setup.config.slug])

    selected = setup.launcher.select(setup.config, "master", {"id": "master"})

    assert isinstance(selected, Selection)
    assert (selected.hive, selected.route_kind) == ("remote", "subscription")


def test_hive_launch_keeps_masters_of_an_unnamed_swarm_off_a_headless_hive(setup):
    from scripts.swarm.profile_choice import ProfileDecision

    setup.herdr._profile_decision.return_value = ProfileDecision("master", "lane", "master")
    write_hive(setup, excluded=["master"], master_swarms=[SCRATCH])

    assert setup.launcher.select(setup.config, "master", {"id": "master"}) == placement.Wait(
        "no live hive takes master"
    )


def test_a_master_moves_only_to_a_hive_that_names_its_swarm(setup):
    from types import SimpleNamespace

    from scripts.swarm import lease, master_move

    now = lease.now_ms(setup.store)
    write_hive(setup, excluded=["master"], master_swarms=[setup.config.slug])
    master = SimpleNamespace(harness="claude", account="account", route_kind="subscription")

    assert master_move.target(setup.store, setup.config, master, now) == "remote"
    write_hive(setup, excluded=["master"], master_swarms=[SCRATCH])
    assert master_move.target(setup.store, setup.config, master, now) == placement.Wait("no live hive takes master")
