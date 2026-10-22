import json
import re
import socket

import pytest

from scripts.hive import cli, registry
from scripts.hive.auth import HiveError
from scripts.swarm import commands, lease
from scripts.swarm.store import RedisStore
from scripts.swarm_v2.keyspace import installation

DEFAULT = {
    "id": "box",
    "name": "box",
    "ui": "no",
    "ephemeral": "no",
    "excluded": ["master"],
    "prefer": {},
    "max_agents": 20,
    "harnesses": [],
    "slots": [],
    "interactive": {"claude": "", "codex": ""},
    "master_login": {"claude": ""},
    "repos": [],
    "heartbeat_at": 0,
    "version": "",
}
HEADED = {**DEFAULT, "ui": "yes", "excluded": [], "max_agents": 10}


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


def test_a_new_hive_gets_every_field_and_joins_the_index(redis):
    assert registry.update(redis, "box", ["max-agents=3"]) == {**DEFAULT, "max_agents": 3}
    assert registry.show(redis, "box") == {**DEFAULT, "max_agents": 3}
    assert redis.smembers(f"{registry.ROOT}:hives") == {"box"}
    stored = redis.hgetall(f"{registry.ROOT}:hive:box")
    assert "roles" not in stored and "excluded" not in stored
    assert stored["interactive"] == '{"claude": "", "codex": ""}'
    assert stored["max_agents"] == "3"


def test_a_member_updates_its_settings_without_writing_the_controller_index(redis, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_HIVE_ID", "box")
    monkeypatch.setenv("AGENTIHOOKS_HIVE_REDIS_URL", "redis://localhost:6379/15")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_REDIS_URL", "redis://localhost:6379/15")
    assert registry.update(redis, "box", ["ui=no", "ephemeral=yes", "excluded=master,qa"]) == {
        **DEFAULT,
        "ui": "no",
        "ephemeral": "yes",
        "excluded": ["master", "qa"],
    }
    assert registry.show(redis, "box")["excluded"] == ["master", "qa"]
    assert not redis.exists(registry.INDEX)


def test_settings_merge_onto_the_stored_record_and_keep_reported_fields(redis):
    registry.update(redis, "box", ["ui=yes", "excluded=qa", "prefer=master:1,eng:2"])
    redis.hset(f"{registry.ROOT}:hive:box", mapping={"heartbeat_at": "500", "slots": '["a"]', "version": "2.4"})
    record = registry.update(redis, "box", ["name=Laptop", "ephemeral=yes", "max-agents=4"])
    expected = {
        **DEFAULT,
        "name": "Laptop",
        "ui": "yes",
        "ephemeral": "yes",
        "excluded": ["qa"],
        "prefer": {"master": 1, "eng": 2},
        "max_agents": 4,
        "slots": ["a"],
        "heartbeat_at": 500,
        "version": "2.4",
    }
    assert record == expected
    assert registry.show(redis, "box") == expected


def test_a_restarted_registration_keeps_the_stored_settings(redis):
    pod = ["ui=no", "ephemeral=yes", "excluded=master", "max-agents=9"]
    first = registry.update(redis, "box", pod, keep=True)
    assert first == {**DEFAULT, "ephemeral": "yes", "max_agents": 9}
    registry.update(redis, "box", ["max-agents=3", "excluded=qa"])
    restarted = registry.update(redis, "box", [*pod, "name=Pod"], keep=True)
    expected = {**DEFAULT, "name": "Pod", "ephemeral": "yes", "excluded": ["qa"], "max_agents": 3}
    assert restarted == expected
    assert registry.show(redis, "box") == expected
    assert redis.hget(registry.key("box"), "max_agents") == "3"


def test_a_restarted_registration_keeps_a_stored_ui_and_ephemeral(redis):
    registry.update(redis, "box", ["ui=yes", "ephemeral=no"])
    restarted = registry.update(redis, "box", ["ui=no", "ephemeral=yes", "name=Pod"], keep=True)
    assert (restarted["ui"], restarted["ephemeral"], restarted["name"]) == ("yes", "no", "Pod")
    assert redis.hmget(registry.key("box"), "ui", "ephemeral", "name") == ["yes", "no", "Pod"]


def test_a_restarted_registration_with_nothing_new_writes_nothing(redis):
    registry.update(redis, "box", ["max-agents=3"])
    assert registry.update(redis, "box", ["max-agents=9"], keep=True) == {**DEFAULT, "max_agents": 3}
    assert redis.hget(registry.key("box"), "max_agents") == "3"


def test_empty_excluded_and_prefer_clear_them(redis):
    registry.update(redis, "box", ["excluded=eng,ci,", "prefer=plan:1,,dispatch:2"])
    assert registry.show(redis, "box")["prefer"] == {"plan": 1, "dispatch": 2}
    assert registry.show(redis, "box")["excluded"] == ["eng", "ci"]
    record = registry.update(redis, "box", ["prefer=", "excluded="])
    assert (record["excluded"], record["prefer"]) == ([], {})


def _race_once(monkeypatch, field, value):
    read = registry._stored
    pending = [(field, value)]

    def racing(client, hive_id):
        record = read(client, hive_id)
        if pending:
            client.hset(registry.key(hive_id), *pending.pop())
        return record

    monkeypatch.setattr(registry, "_stored", racing)
    return read


def test_a_setting_keeps_a_heartbeat_written_while_it_was_checked(redis, monkeypatch):
    registry.update(redis, "box", ["ui=yes"])
    _race_once(monkeypatch, "heartbeat_at", "900")
    registry.update(redis, "box", ["name=Laptop"])
    assert redis.hget(registry.key("box"), "heartbeat_at") == "900"
    assert redis.hget(registry.key("box"), "name") == "Laptop"


def test_a_new_hive_keeps_a_heartbeat_written_while_it_was_checked(redis, monkeypatch):
    _race_once(monkeypatch, "heartbeat_at", "900")
    assert registry.update(redis, "box", ["ui=yes"]) == {**HEADED, "heartbeat_at": 900}
    assert registry.show(redis, "box") == {**HEADED, "heartbeat_at": 900}


def test_a_setting_is_checked_again_against_a_concurrent_change(redis, monkeypatch):
    registry.update(redis, "box", ["ui=yes"])
    _race_once(monkeypatch, "ui", "no")
    with pytest.raises(HiveError) as error:
        registry.update(redis, "box", ["prefer=master:1"])
    assert str(error.value) == "prefer names master, a role this hive excludes"
    assert redis.hget(registry.key("box"), "prefer") == "{}"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("excluded", "eng"),
        ("max_agents", "many"),
        ("excluded", "5"),
        ("prefer", "[]"),
        ("harnesses", "{}"),
        ("slots", '"a"'),
        ("interactive", "[]"),
        ("repos", "null"),
    ],
)
def test_an_unreadable_stored_field_is_refused_by_name(redis, field, value):
    redis.hset(registry.key("box"), field, value)
    with pytest.raises(HiveError) as error:
        registry.show(redis, "box")
    assert str(error.value) == f"hive box holds an unreadable {field}: {value!r}"


def test_a_value_may_hold_an_equals_sign(redis):
    assert registry.update(redis, "box", ["name=a=b"])["name"] == "a=b"


def test_repeated_excluded_names_are_kept_once(redis):
    assert registry.update(redis, "box", ["excluded=eng,ci,eng"])["excluded"] == ["eng", "ci"]


def test_a_record_written_by_another_writer_shows_with_defaults(redis):
    redis.hset(f"{registry.ROOT}:hive:pod", mapping={"id": "pod", "heartbeat_at": "7"})
    assert registry.show(redis, "pod") == {**DEFAULT, "id": "pod", "name": "pod", "heartbeat_at": 7}
    assert registry.show(redis, "absent") is None


@pytest.mark.parametrize(
    ("settings", "reason"),
    [
        ([], "hive set needs at least one setting"),
        (
            ["colour=red"],
            "unknown setting 'colour=red'; settings are name, ui, ephemeral, draining, excluded, prefer, max-agents",
        ),
        (["ui"], "unknown setting 'ui'; settings are name, ui, ephemeral, draining, excluded, prefer, max-agents"),
        (["ui=maybe"], "ui must be yes or no, not 'maybe'"),
        (["ephemeral=true"], "ephemeral must be yes or no, not 'true'"),
        (["name="], "name must not be empty"),
        (["excluded=eng,a:b"], "excluded takes lane roles or profiles, not 'a:b'"),
        (["max-agents=0"], "max-agents must be a positive whole number, not '0'"),
        (["max-agents=two"], "max-agents must be a positive whole number, not 'two'"),
        (["max-agents=-1"], "max-agents must be a positive whole number, not '-1'"),
        (["max-agents=²"], "max-agents must be a positive whole number, not '²'"),
        (["prefer=eng"], "prefer takes role:rank, not 'eng'"),
        (["prefer=eng:0"], "the rank of eng must be a positive whole number, not '0'"),
        (["prefer=eng:1:2"], "the rank of eng must be a positive whole number, not '1:2'"),
        (["prefer=master:1"], "prefer names master, a role this hive excludes"),
    ],
)
def test_invalid_settings_are_refused_and_nothing_is_written(redis, settings, reason):
    with pytest.raises(HiveError) as error:
        registry.update(redis, "box", settings)
    assert str(error.value) == reason
    assert redis.keys("*") == []


@pytest.mark.parametrize("hive_id", ["", "-box", "a:b", "a b", "x" * 64])
def test_a_malformed_hive_id_is_refused(redis, hive_id):
    with pytest.raises(HiveError) as error:
        registry.update(redis, hive_id, ["ui=yes"])
    assert str(error.value) == f"hive id {hive_id!r} must be letters, digits, dots, dashes or underscores"
    assert redis.keys("*") == []


def test_the_longest_hive_id_is_accepted(redis):
    assert registry.update(redis, "a._-" + "x" * 59, ["ui=yes"])["ui"] == "yes"


def test_liveness_follows_the_heartbeat_window():
    beat = {"heartbeat_at": 1_000}
    assert registry.live(beat, 1_000) is True
    assert registry.live(beat, 1_000 + registry.LIVE_MS) is True
    assert registry.live(beat, 1_001 + registry.LIVE_MS) is False
    assert registry.live({"heartbeat_at": 0}, 5) is False
    assert registry.live({"heartbeat_at": 1}, 5) is True
    assert registry.LIVE_MS == 90_000


def test_hives_lists_indexed_records_sorted_and_skips_missing(redis):
    names = ["zeta", "alpha", "mu", "kappa", "omega", "beta", "delta", "sigma"]
    for name in names:
        registry.update(redis, name, ["ui=yes"])
    redis.sadd(f"{registry.ROOT}:hives", "gone")
    assert [record["id"] for record in registry.hives(redis)] == sorted(names)


def test_cli_set_show_and_list(redis, monkeypatch, capsys):
    monkeypatch.setattr(cli, "redis_client", lambda: redis)
    monkeypatch.setattr(cli, "now_ms", lambda: 100_000)
    assert cli.main(["set", "box", "ui=yes", "excluded=qa", "prefer=master:1", "max-agents=3"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["excluded"] == ["qa"]
    assert printed["max_agents"] == 3
    registry.update(redis, "pod", ["excluded=eng"])
    redis.hset(f"{registry.ROOT}:hive:box", "heartbeat_at", "50000")
    assert cli.main(["show", "box"]) == 0
    assert json.loads(capsys.readouterr().out)["heartbeat_at"] == 50_000
    assert cli.main(["list"]) == 0
    assert capsys.readouterr().out == (
        "box\tlive\tui=yes\tmax-agents=3\texcluded=qa\theadroom=missing\n"
        "pod\tstale\tui=no\tmax-agents=20\texcluded=eng\theadroom=missing\n"
    )


def test_cli_set_keep_leaves_stored_settings(redis, monkeypatch, capsys):
    monkeypatch.setattr(cli, "redis_client", lambda: redis)
    registry.update(redis, "box", ["max-agents=3"])
    assert cli.main(["set", "--keep", "box", "max-agents=9", "name=Pod"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert (printed["max_agents"], printed["name"]) == (3, "Pod")
    assert cli.main(["set", "box", "max-agents=9"]) == 0
    assert json.loads(capsys.readouterr().out)["max_agents"] == 9


def test_cli_refusals_exit_one_with_the_reason(redis, monkeypatch, capsys):
    monkeypatch.setattr(cli, "redis_client", lambda: redis)
    assert cli.main(["set", "box", "prefer=master:1"]) == 1
    assert capsys.readouterr().err == "hive set refused: prefer names master, a role this hive excludes\n"
    assert cli.main(["show", "box"]) == 1
    assert capsys.readouterr().err == "hive show refused: no hive box\n"


def test_cli_now_ms_reads_the_wall_clock(monkeypatch):
    monkeypatch.setattr(cli.time, "time_ns", lambda: 7_000_000_999_999)
    assert cli.now_ms() == 7_000_000


def test_cli_set_takes_any_number_of_key_value_settings(redis, monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "300")
    parser = cli._parser()._subparsers._group_actions[0].choices["set"]
    assert parser.format_usage() == "usage: agentihooks hive set [-h] [--keep] id [key=value ...]\n"
    keep = r"(?m)^  --keep +Leave max-agents, excluded, ui and ephemeral as stored$"
    assert re.search(keep, parser.format_help())
    monkeypatch.setattr(cli, "redis_client", lambda: redis)
    assert cli.main(["set", "box"]) == 1
    assert capsys.readouterr().err == "hive set refused: hive set needs at least one setting\n"


def test_cli_list_joins_excluded_with_commas(redis, monkeypatch, capsys):
    monkeypatch.setattr(cli, "redis_client", lambda: redis)
    monkeypatch.setattr(cli, "now_ms", lambda: 0)
    registry.update(redis, "box", ["excluded=eng,ci"])
    assert cli.main(["list"]) == 0
    assert capsys.readouterr().out == "box\tstale\tui=no\tmax-agents=20\texcluded=eng,ci\theadroom=missing\n"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_HIVE_ID", raising=False)
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    return tmp_path


def test_hive_id_prefers_the_environment(home, monkeypatch):
    (home / "installation.json").write_text(json.dumps({"hive_id": "seeded"}))
    monkeypatch.setenv("SWARM_HIVE_ID", "env-box")
    assert commands.hive_id() == "env-box"


def test_hive_id_is_seeded_from_the_installation_record(home):
    (home / "installation.json").write_text(json.dumps({"installation_id": "x", "hive_id": "seeded.box"}))
    assert commands.hive_id() == "seeded.box"


def test_hive_id_reads_the_default_home(tmp_path, monkeypatch):
    monkeypatch.delenv("SWARM_HIVE_ID", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_HOME", raising=False)
    monkeypatch.setattr(registry.Path, "home", lambda: tmp_path)
    (tmp_path / ".agentihooks").mkdir()
    (tmp_path / ".agentihooks" / "installation.json").write_text(json.dumps({"hive_id": "home-box"}))
    assert commands.hive_id() == "home-box"


@pytest.mark.parametrize(
    "content",
    [None, "not json", "[]", json.dumps({"installation_id": "x"}), json.dumps({"hive_id": 5}), '{"hive_id": "a:b"}'],
)
def test_hive_id_falls_back_to_the_hostname_without_a_seed(home, content):
    if content is not None:
        (home / "installation.json").write_text(content)
    assert commands.hive_id() == socket.gethostname()
    assert (home / "installation.json").exists() is (content is not None)


def test_an_existing_swarm_keeps_its_owner(home, monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    installation(home)
    monkeypatch.setattr(lease, "now_ms", lambda saved: 1000)
    held = lease.acquire(store, "sw", socket.gethostname())
    assert lease.acquire(store, "sw", commands.hive_id()) == lease.Lease(held.owner, held.epoch, held.expires_at)
    assert lease.current(store, "sw").owner == socket.gethostname()
