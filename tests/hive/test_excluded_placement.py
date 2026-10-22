import pytest

from scripts.hive import cli, placement, registry
from scripts.hive.auth import HiveError
from tests.hive.test_controller_launch import setup as setup
from tests.hive.test_placement import NOW, REPO, hive


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


def headless(hive_id="anton", **fields):
    return hive(hive_id, **{"ui": "no", "excluded": ["master"], "max_agents": 20, **fields})


def headed(hive_id="workstation", **fields):
    return hive(hive_id, **{"max_agents": 10, **fields})


def test_a_headless_hive_defaults_to_twenty_agents_and_excludes_the_master(redis):
    record = registry.update(redis, "anton", ["ui=no"])
    assert (record["max_agents"], record["excluded"]) == (20, ["master"])
    assert "max_agents" not in redis.hgetall(registry.key("anton"))
    assert "excluded" not in redis.hgetall(registry.key("anton"))


def test_a_headed_hive_defaults_to_ten_agents_and_excludes_nothing(redis):
    record = registry.update(redis, "root", ["ui=yes"])
    assert (record["max_agents"], record["excluded"]) == (10, [])
    assert (registry.show(redis, "root")["max_agents"], registry.show(redis, "root")["excluded"]) == (10, [])


def test_the_defaults_follow_a_later_ui_change(redis):
    registry.update(redis, "box", ["ui=no"])
    record = registry.update(redis, "box", ["ui=yes"])
    assert (record["max_agents"], record["excluded"]) == (10, [])


def test_excluded_and_max_agents_are_set_explicitly(redis):
    record = registry.update(redis, "box", ["max-agents=12", "excluded=master,frontend,qa"])
    assert (record["max_agents"], record["excluded"]) == (12, ["master", "frontend", "qa"])
    assert registry.update(redis, "box", ["excluded="])["excluded"] == []


def test_excluded_refuses_a_malformed_name(redis):
    with pytest.raises(HiveError) as error:
        registry.update(redis, "box", ["excluded=eng,bad name"])
    assert str(error.value) == "excluded takes lane roles or profiles, not 'bad name'"


def test_roles_is_no_longer_a_setting(redis):
    with pytest.raises(HiveError) as error:
        registry.update(redis, "box", ["roles=eng"])
    assert str(error.value).startswith("unknown setting 'roles=eng'")


def test_prefer_cannot_name_an_excluded_role(redis):
    with pytest.raises(HiveError) as error:
        registry.update(redis, "box", ["ui=no", "prefer=master:1"])
    assert str(error.value) == "prefer names master, a role this hive excludes"


def test_a_hive_stored_with_roles_moves_to_the_new_rule_on_its_next_set(redis):
    redis.hset(registry.key("root"), mapping={"ui": "yes", "roles": '["master", "eng"]', "max_agents": "16"})
    record = registry.update(redis, "root", ["name=Root"])
    assert (record["max_agents"], record["excluded"]) == (10, [])
    stored = redis.hgetall(registry.key("root"))
    assert "roles" not in stored and "max_agents" not in stored
    assert "roles" not in record


def test_a_hive_stored_with_roles_reads_under_the_new_rule_before_it_moves(redis):
    redis.hset(registry.key("anton"), mapping={"ui": "no", "roles": '["eng"]', "max_agents": "1"})
    record = registry.show(redis, "anton")
    assert (record["max_agents"], record["excluded"]) == (20, ["master"])
    assert "roles" not in record


def test_a_heartbeat_moves_a_hive_stored_with_roles_to_the_new_rule(redis):
    redis.hset(registry.key("anton"), mapping={"ui": "no", "roles": '["eng"]', "max_agents": "1", "name": "Anton"})
    registry.migrate(redis, "anton")
    stored = redis.hgetall(registry.key("anton"))
    assert "roles" not in stored and "max_agents" not in stored
    assert stored["name"] == "Anton"
    assert registry.show(redis, "anton")["max_agents"] == 20


def test_a_heartbeat_keeps_an_explicit_max_agents(redis):
    registry.update(redis, "anton", ["ui=no", "max-agents=4"])
    registry.migrate(redis, "anton")
    with redis.pipeline() as pipe:
        registry.seed(pipe, "anton")
        pipe.execute()
    assert registry.show(redis, "anton")["max_agents"] == 4


def test_hive_list_shows_max_agents_and_excluded(redis, monkeypatch, capsys):
    monkeypatch.setattr(cli, "redis_client", lambda: redis)
    monkeypatch.setattr(cli, "now_ms", lambda: 0)
    registry.update(redis, "anton", ["ui=no"])
    registry.update(redis, "root", ["ui=yes"])
    assert cli.main(["list"]) == 0
    assert capsys.readouterr().out == (
        "anton\tstale\tui=no\tmax-agents=20\texcluded=master\theadroom=missing\n"
        "root\tstale\tui=yes\tmax-agents=10\texcluded=none\theadroom=missing\n"
    )


@pytest.mark.parametrize("role", ["ci", "plan"])
def test_a_ci_and_a_plan_agent_land_on_a_headless_hive(role):
    hives = [headless(heartbeat_at=NOW)]
    assert placement.place(role, "claude", "acct", "subscription", REPO, hives, NOW) == "anton"


def test_a_master_does_not_land_on_a_default_headless_hive():
    hives = [headless(heartbeat_at=NOW)]
    assert placement.place("master", "claude", "acct", "subscription", REPO, hives, NOW) == placement.Wait(
        "no live hive takes master"
    )
    assert isinstance(placement.place_master("claude", "", REPO, hives, NOW), placement.Wait)


def test_a_master_lands_on_a_headed_hive_beside_a_headless_one():
    hives = [headless(heartbeat_at=NOW), headed(heartbeat_at=NOW)]
    assert placement.place("master", "claude", "acct", "subscription", REPO, hives, NOW) == "workstation"
    assert placement.place_master("claude", "", REPO, hives, NOW).hive == "workstation"


def test_a_ui_role_falls_back_to_a_headless_hive_when_no_headed_hive_is_live():
    hives = [headless(heartbeat_at=NOW)]
    assert placement.choose("qa", "claude", "acct", "subscription", REPO, hives, NOW) == placement.Choice(
        "anton", "no live UI hive takes qa"
    )


def test_a_profile_or_lane_in_the_excluded_list_keeps_the_agent_off_that_hive():
    assert not placement.hosts("frontend", headless(excluded=["frontend"]), ("eng", "frontend"))
    assert not placement.hosts("eng", headed(excluded=["eng"]), ("eng", "engineer"))
    assert not placement.hosts("frontend", headed(excluded=["eng"]), ("eng", "frontend"))
    assert placement.hosts("eng", headless(excluded=["frontend"]), ("eng", "engineer"))


def test_a_hive_never_takes_an_agent_past_max_agents():
    hives = [headless(heartbeat_at=NOW, max_agents=20, agents=20)]
    assert placement.place("ci", "claude", "acct", "subscription", REPO, hives, NOW) == placement.Wait(
        "no hive for ci has room"
    )


@pytest.mark.parametrize(
    ("lane", "profile", "excluded", "expected"),
    [
        ("eng", "frontend", ["frontend"], "no live hive takes frontend"),
        ("eng", "engineer", ["eng"], "no live hive takes eng"),
        ("eng", "frontend", [], "remote"),
        ("ci", "cicd", ["master"], "remote"),
        ("eng", "planner", ["plan"], "no live hive takes plan"),
        ("eng", "planner", ["eng"], "no live hive takes plan"),
        ("ci", "engineer", ["engineer"], "no live hive takes eng"),
    ],
)
def test_a_launch_keeps_an_agent_off_a_hive_that_excludes_its_lane_or_profile(setup, lane, profile, excluded, expected):
    from scripts.swarm.profile_choice import ProfileDecision
    from tests.hive.test_controller_launch import write_hive

    setup.herdr._profile_decision.return_value = ProfileDecision(profile, "task", profile)
    write_hive(setup, ui="no", excluded=excluded)
    selected = setup.launcher.select(setup.config, lane, {"id": "task"})
    assert getattr(selected, "hive", None) == expected or selected == placement.Wait(expected)
