import json
from unittest.mock import Mock

import pytest
from nats.js.errors import BucketNotFoundError

from scripts import session_bands
from scripts.hive import account_slots, daemon, registry

pytestmark = pytest.mark.unit

PUBLISH_HEADROOM = daemon.publish_headroom


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


def test_beat_refreshes_reported_fields_and_expires_telemetry(redis, monkeypatch):
    registry.update(redis, "machine", ["name=Worker", "max-agents=8"])
    report = {
        "slots": [{"harness": "claude", "account": "one", "kind": "subscription", "cap": 4}],
        "interactive": {"claude": "", "codex": "default"},
        "repos": ["/repos/project"],
        "sessions": {"claude:one": 2},
        "quota": {
            "claude:one": {
                "five_hour": {"used": 22, "resets_at": 800},
                "seven_day": {"used": 36, "resets_at": 900},
                "observed_at": 120,
            }
        },
    }
    monkeypatch.setattr(daemon, "observe", lambda **kwargs: report)
    monkeypatch.setattr(daemon.time, "time", lambda: 123.456)
    daemon.beat(redis, "machine")
    from scripts.hive import orders

    assert redis.ttl(orders.Orders(redis).pod("machine", orders.POD)) == orders.POD_SECONDS
    record = registry.show(redis, "machine")
    assert record["name"] == "Worker"
    assert record["max_agents"] == 8
    assert record["heartbeat_at"] == 123456
    for field in ("slots", "interactive", "repos"):
        assert record[field] == report[field]
    for field in ("sessions", "quota"):
        key = f"{registry.key('machine')}:{field}"
        assert json.loads(redis.get(key)) == report[field]
        assert redis.ttl(key) == 45
    report.update(slots=[], repos=[], sessions={}, quota={})
    daemon.beat(redis, "machine")
    assert registry.show(redis, "machine")["slots"] == []
    assert json.loads(redis.get(f"{registry.key('machine')}:sessions")) == {}


def test_hive_heartbeat_renews_account_counters_from_its_own_quota_and_executions(redis, monkeypatch):
    from scripts.hive import account_slots

    report = {"slots": [], "interactive": {}, "repos": [], "sessions": {}, "quota": {}}
    hosted = {"executions": {}, "names": []}
    publish = Mock()
    monkeypatch.setattr(daemon, "observe", lambda **kwargs: report)
    monkeypatch.setattr(daemon, "execution_report", lambda *args: hosted)
    monkeypatch.setattr(account_slots, "publish", publish)
    daemon.beat(redis, "machine")
    publish.assert_called_once_with("machine", report, hosted)


@pytest.mark.parametrize(
    "failure",
    [
        BucketNotFoundError(),
        TimeoutError("no responders for 'swarm.hive.slots'"),
        account_slots.Unavailable("no account slot left"),
    ],
)
def test_hive_heartbeat_keeps_beating_when_the_account_slot_publish_fails(redis, monkeypatch, capsys, failure):
    report = {"slots": [], "interactive": {}, "repos": [], "sessions": {}, "quota": {}}
    hosted = {"executions": {}, "names": ["kept"]}
    publish_config, close_dead_tabs = Mock(), Mock()
    monkeypatch.setattr(daemon, "observe", lambda **kwargs: report)
    monkeypatch.setattr(daemon, "execution_report", lambda *args: hosted)
    monkeypatch.setattr(account_slots, "publish", Mock(side_effect=failure))
    monkeypatch.setattr(daemon, "publish_config", publish_config)
    monkeypatch.setattr(daemon, "close_dead_tabs", close_dead_tabs)
    daemon.beat(redis, "machine")
    publish_config.assert_called_once_with(redis, "machine", [])
    close_dead_tabs.assert_called_once_with({"kept"})
    assert "account slot heartbeat failed" in capsys.readouterr().err


def test_cold_native_accounts_use_their_own_fresh_quota_before_the_first_launch(monkeypatch):
    from hooks.context import quota_usage
    from scripts.routing import master_account

    monkeypatch.setattr(daemon.balancer, "cached_observations", lambda **kwargs: [])
    monkeypatch.setattr(daemon.balancer, "discover_credentials", lambda env: [])
    monkeypatch.setattr(daemon, "_claude_snapshots", lambda observed: None)
    monkeypatch.setattr(daemon.master_claude_login, "logins", lambda: {1: "login"})
    monkeypatch.setattr(
        master_account, "load", lambda environ: {"claude": master_account.MasterAccount("claude", "native")}
    )
    monkeypatch.setattr(daemon, "claude_signed_in", lambda: False)
    monkeypatch.setattr(daemon.codex_router, "accounts", lambda env: [])
    monkeypatch.setattr(daemon.codex_router, "quotas", lambda *args: {})
    monkeypatch.setattr(
        quota_usage,
        "latest_snapshot",
        lambda account: (
            99,
            {
                "five_hour": {"used_percentage": 1, "resets_at": 500},
                "seven_day": {"used_percentage": 2, "resets_at": 900},
            },
        ),
    )
    slots, readings = daemon._subscriptions({}, {"claude": {}, "codex": {}}, 100)
    assert {slot["account"] for slot in slots} == {"login", "native"}
    assert all(slot["cap"] > 0 for slot in slots)
    assert readings["claude:login"]["observed_at"] == 99
    assert readings["claude:native"]["observed_at"] == 99
    for account in ("login", "native"):
        assert readings[f"claude:{account}"]["five_hour"]["resets_at"] == 500
        assert readings[f"claude:{account}"]["seven_day"]["resets_at"] == 900


def test_native_snapshot_collection_continues_after_a_missing_account_reading(monkeypatch):
    from hooks.context import quota_usage
    from scripts.routing import master_account

    monkeypatch.setattr(daemon.master_claude_login, "logins", lambda: {1: "one", 2: "two"})
    monkeypatch.setattr(master_account, "load", lambda env: {})
    seen = []

    def snapshot(account):
        assert account in {"one", "two"}
        seen.append(account)
        return (
            None
            if len(seen) == 1
            else (0.5, {"five_hour": {"used_percentage": 1}, "seven_day": {"used_percentage": 2}})
        )

    monkeypatch.setattr(quota_usage, "latest_snapshot", snapshot)
    observed = {}
    assert daemon._native_accounts({}, observed) == {"one", "two"}
    assert len(seen) == 2
    assert set(observed) == {seen[1]}
    assert observed[seen[1]][0] == 0.5


def test_native_snapshot_updates_preserve_equal_cache_and_continue_to_newer_readings(monkeypatch):
    from hooks.context import quota_usage
    from scripts.routing import master_account

    monkeypatch.setattr(daemon.master_claude_login, "logins", lambda: {1: "one", 2: "two"})
    monkeypatch.setattr(master_account, "load", lambda env: {})
    seen = []
    original = object()
    observed = {account: (100, original) for account in ("one", "two")}

    def snapshot(account):
        assert account in observed
        seen.append(account)
        return (
            100 if len(seen) == 1 else 101,
            {"five_hour": {"used_percentage": 1}, "seven_day": {"used_percentage": 2}},
        )

    monkeypatch.setattr(quota_usage, "latest_snapshot", snapshot)
    assert daemon._native_accounts({}, observed) == {"one", "two"}
    assert len(seen) == 2
    assert observed[seen[0]] == (100, original)
    assert observed[seen[1]][0] == 101
    assert observed[seen[1]][1].five_hour.used == 1


def test_three_missed_beats_expire_both_keys(redis, monkeypatch):
    import time

    monkeypatch.setattr(
        daemon, "observe", lambda **kwargs: {"slots": [], "interactive": {}, "repos": [], "sessions": {}, "quota": {}}
    )
    daemon.beat(redis, "machine")
    future = time.time() + 46
    with monkeypatch.context() as patcher:
        patcher.setattr(time, "time", lambda: future)
        assert redis.get(f"{registry.key('machine')}:sessions") is None
        assert redis.get(f"{registry.key('machine')}:quota") is None
    assert redis.exists(registry.key("machine"))


@pytest.mark.parametrize("account", ["login", ""])
@pytest.mark.parametrize("enabled", [True, False])
def test_beat_publishes_the_master_login_account(redis, monkeypatch, account, enabled):
    monkeypatch.setattr(
        daemon, "observe", lambda **kwargs: {"slots": [], "interactive": {}, "repos": [], "sessions": {}, "quota": {}}
    )
    monkeypatch.setattr(daemon.master_claude_login, "signed_in", lambda: account)
    monkeypatch.setattr(daemon.master_claude_login, "unusable", lambda: "" if enabled else "off")
    daemon.beat(redis, "machine")
    assert registry.show(redis, "machine")["master_login"] == {"claude": account}
    assert registry.show(redis, "machine")["master_login_enabled"] == ("yes" if enabled else "no")


@pytest.mark.parametrize(("member", "indexed", "ttl"), [("machine", set(), 10), ("other", {"machine"}, 45)])
def test_a_member_beat_leaves_the_index_and_launch_slots_to_the_controller(redis, monkeypatch, member, indexed, ttl):
    import time

    from scripts.routing.reservation import LaunchSlots
    from scripts.swarm.keyspace import ROOT

    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    monkeypatch.setenv("AGENTIHOOKS_HIVE_ID", member)
    monkeypatch.setattr(
        daemon, "observe", lambda **kwargs: {"slots": [], "interactive": {}, "repos": [], "sessions": {}, "quota": {}}
    )
    monkeypatch.setattr(daemon, "_executions", lambda redis, local_id, processes: {"claude:one": ["first"]})
    slots = LaunchSlots(redis)
    assert slots.reserve("claude:one", "first", 1, 10000)
    assert slots.acknowledge("first")
    redis.pexpire(f"{ROOT}:launch-slot:first", 10000)

    daemon.beat(redis, "machine")

    assert redis.smembers(registry.INDEX) == indexed
    assert ttl - 5 < redis.pttl(f"{ROOT}:launch-slot:first") / 1000 <= ttl


def _once(connection):
    return Mock(side_effect=[(connection, connection), AssertionError("reconnected")])


def _quiet_quota(monkeypatch):
    monkeypatch.setattr(daemon.quota_refresh, "refresh", lambda environ: None)
    monkeypatch.setattr(daemon.quota_refresh, "start", lambda environ, worker, attempted_at: (None, attempted_at))


def test_run_beats_immediately_then_waits_a_beat_less_the_wake_slack(monkeypatch):
    events = []
    connection = object()
    monkeypatch.setattr(daemon, "beat", lambda redis, hive: events.append((redis, hive)))
    monkeypatch.setattr(daemon.quota_refresh, "refresh", lambda environ: events.append("refresh"))
    monkeypatch.setattr(
        daemon.quota_refresh,
        "start",
        lambda environ, worker, attempted_at: (
            events.append(("start", environ, worker, attempted_at)) or ("worker", attempted_at + 1)
        ),
    )
    monkeypatch.setattr(daemon, "hive_id", lambda: "local")
    monkeypatch.setattr(daemon.time, "monotonic", lambda: 0)
    monkeypatch.setattr(daemon.time, "time", lambda: 50)

    waits = []

    def sleep(seconds, redis, timeout):
        assert redis is connection
        assert timeout == seconds + 1
        events.append(seconds)
        waits.append(seconds)
        if len(waits) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(daemon, "scheduled_wait", sleep)
    with pytest.raises(KeyboardInterrupt):
        daemon.run(_once(connection))
    environ = daemon.os.environ
    assert events == [
        "refresh",
        ("start", environ, None, 50),
        (connection, "local"),
        13,
        ("start", environ, "worker", 51),
        (connection, "local"),
        13,
    ]


def test_run_deducts_observation_time_from_the_beat_interval(monkeypatch):
    monkeypatch.setattr(daemon, "hive_id", lambda: "local")
    monkeypatch.setattr(daemon, "beat", lambda redis, hive: None)
    monkeypatch.setattr(daemon.quota_refresh, "refresh", lambda environ: None)
    monkeypatch.setattr(daemon.quota_refresh, "start", lambda environ, worker, attempted_at: (None, attempted_at))
    timestamps = iter([100, 103])
    monkeypatch.setattr(daemon.time, "monotonic", lambda: next(timestamps))
    sleep = Mock(side_effect=KeyboardInterrupt)
    monkeypatch.setattr(daemon, "scheduled_wait", sleep)
    waiter = object()
    with pytest.raises(KeyboardInterrupt):
        daemon.run(Mock(return_value=(object(), waiter)))
    sleep.assert_called_once_with(12, redis=waiter, timeout=12)


def _paced(monkeypatch, rounds):
    import threading

    from scripts.hive import orders

    stop, waits, beats = threading.Event(), [], []
    monkeypatch.setattr(orders, "alive", lambda client, hive: beats.append((client, hive)))

    def wait(seconds, redis, timeout):
        waits.append((seconds, redis, timeout))
        if len(waits) == rounds:
            stop.set()

    monkeypatch.setattr(daemon, "scheduled_wait", wait)
    return stop, waits, beats


def test_headroom_publishes_on_the_scheduler_cadence_until_stopped(monkeypatch):
    from scripts.hive import headroom

    calls = []
    monkeypatch.setattr(headroom, "publish", lambda client, hive, now_ms: calls.append((client, hive, now_ms)))
    monkeypatch.setattr(daemon.time, "time", lambda: 7.5)
    stop, waits, beats = _paced(monkeypatch, 3)
    connection, waiter = object(), object()
    PUBLISH_HEADROOM(connection, waiter, "local", stop)
    assert calls == [(connection, "local", 7_500), (connection, "local", 7_500)]
    assert beats == [(connection, "local"), (connection, "local")]
    assert waits == [(daemon.HEADROOM_SECONDS, waiter, daemon.HEADROOM_SECONDS + daemon.WAKE_SLACK_S)] * 3
    assert (daemon.HEADROOM_SECONDS + daemon.WAKE_SLACK_S) * 1000 < headroom.STALE_MS


@pytest.mark.parametrize("error", ["ConnectionError", "TimeoutError", "ResponseError", "JSONDecodeError", "KeyError"])
def test_headroom_keeps_its_cadence_through_a_failed_publish(monkeypatch, capsys, error):
    from redis import exceptions

    from scripts.hive import headroom

    calls = []
    failure = {
        "JSONDecodeError": json.JSONDecodeError("gone", "", 0),
        "KeyError": KeyError("gone"),
    }.get(error) or getattr(exceptions, error)("gone")

    def publish(client, hive, now_ms):
        calls.append(now_ms)
        if len(calls) == 1:
            raise failure

    monkeypatch.setattr(headroom, "publish", publish)
    monkeypatch.setattr(daemon.time, "time", lambda: 1)
    stop, _, beats = _paced(monkeypatch, 3)
    PUBLISH_HEADROOM(object(), object(), "local", stop)
    assert calls == [1_000, 1_000]
    assert len(beats) == 1
    assert capsys.readouterr().err == f"hive headroom publish failed ({error}: {failure})\n"


@pytest.mark.parametrize("error", ["ConnectionError", "TimeoutError"])
def test_headroom_ends_when_its_wait_loses_the_connection(monkeypatch, error):
    import threading

    from redis import exceptions

    from scripts.hive import headroom

    def wait(seconds, redis, timeout):
        raise getattr(exceptions, error)("gone")

    monkeypatch.setattr(daemon, "scheduled_wait", wait)
    monkeypatch.setattr(headroom, "publish", Mock(side_effect=AssertionError("published")))
    PUBLISH_HEADROOM(object(), object(), "local", threading.Event())


def test_headroom_keeps_publishing_while_a_beat_blocks(monkeypatch):
    import threading

    from scripts.hive import headroom

    _quiet_quota(monkeypatch)
    monkeypatch.setattr(daemon, "hive_id", lambda: "local")
    monkeypatch.setattr(daemon, "publish_headroom", PUBLISH_HEADROOM)
    monkeypatch.setattr(daemon, "scheduled_wait", lambda seconds, redis, timeout: threading.Event().wait(0.001))
    published = threading.Event()
    calls = []

    def publish(client, hive, now_ms):
        calls.append((client, hive))
        if len(calls) == 3:
            published.set()

    def beat(redis, hive):
        assert published.wait(5)
        raise KeyboardInterrupt

    from scripts.hive import orders

    connection = object()
    monkeypatch.setattr(headroom, "publish", publish)
    monkeypatch.setattr(orders, "alive", lambda client, hive: None)
    monkeypatch.setattr(daemon, "beat", beat)
    with pytest.raises(KeyboardInterrupt):
        daemon.run(_once(connection))
    assert calls[:3] == [(connection, "local")] * 3


def test_run_publishes_headroom_beside_each_connection_and_stops_it_on_reconnect(monkeypatch):
    from redis.exceptions import ConnectionError

    _quiet_quota(monkeypatch)
    monkeypatch.setattr(daemon, "hive_id", lambda: "local")
    backed_off = []
    monkeypatch.setattr(
        daemon, "_back_off", lambda exc, failures: backed_off.append([t.args[3].is_set() for t in threads])
    )
    first, second, first_waiter, second_waiter = object(), object(), object(), object()
    threads = []

    class Thread:
        def __init__(self, target, args, daemon):
            self.target, self.args, self.daemon = target, args, daemon

        def start(self):
            assert all(thread.args[3].is_set() for thread in threads)
            threads.append(self)

    beats = []

    def beat(redis, hive):
        beats.append(redis)
        if redis is first:
            raise ConnectionError("lost")

    monkeypatch.setattr(daemon.threading, "Thread", Thread)
    monkeypatch.setattr(daemon, "beat", beat)
    monkeypatch.setattr(daemon, "scheduled_wait", Mock(side_effect=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        daemon.run(Mock(side_effect=[(first, first_waiter), (second, second_waiter)]))
    assert beats == [first, second]
    assert [(thread.target, thread.args[:3], thread.daemon) for thread in threads] == [
        (daemon.publish_headroom, (first, first_waiter, "local"), True),
        (daemon.publish_headroom, (second, second_waiter, "local"), True),
    ]
    assert backed_off == [[True]]
    assert threads[1].args[3].is_set()


def test_install_writes_the_service_and_enables_it(tmp_path):
    run = Mock(return_value=Mock(returncode=0))
    assert daemon.install("/bin/agentihooks", tmp_path, run)
    service = tmp_path / "agentihooks-hive.service"
    assert service.read_text() == daemon.unit("/bin/agentihooks")
    assert 'ExecStart="/bin/agentihooks" hive run\n' in service.read_text()
    assert "EnvironmentFile=-%h/.agentihooks/hive.env\n" in service.read_text()
    assert "Restart=always\nRestartSec=5\n" in service.read_text()
    assert run.call_args_list[0].args[0] == ["systemctl", "--user", "daemon-reload"]
    assert run.call_args_list[1].args[0] == ["systemctl", "--user", "enable", "--now", "agentihooks-hive.service"]
    run.reset_mock()
    daemon.install("/bin/agentihooks", tmp_path, run)
    assert run.call_count == 1


def test_install_reports_enable_failure(tmp_path):
    assert daemon.install("/bin/agentihooks", tmp_path, Mock(return_value=Mock(returncode=1))) is False


def test_observe_selects_fields_and_never_publishes_credentials(redis, monkeypatch, tmp_path):
    from scripts import claude_quota_balancer as balancer
    from scripts import codex_quota, codex_router

    sentinel = "credential-sentinel"
    monkeypatch.setattr(daemon, "claude_signed_in", lambda: False)
    monkeypatch.setenv("AH_CC_TOKEN_one", sentinel)
    monkeypatch.setenv("AH_CX_TOKEN_two", sentinel)
    monkeypatch.setenv("ANTHROPIC_API_KEY", sentinel)
    monkeypatch.setenv("OPENAI_API_KEY", sentinel)
    monkeypatch.setattr(daemon.account_sessions, "sessions_by_account", lambda **kwargs: {"one": 2})
    monkeypatch.setattr(daemon.account_sessions, "codex_sessions_by_account", lambda **kwargs: {"two": 3, "default": 1})
    result = balancer.ProbeResult(
        "one", "allowed", "OK", 60, balancer.QuotaWindow(20, 1000), balancer.QuotaWindow(40, 2000), error=sentinel
    )
    monkeypatch.setattr(balancer, "cached_observations", lambda **kwargs: [(120.5, result)])
    monkeypatch.setattr(
        codex_router,
        "accounts",
        lambda environ: [codex_router.CodexAccount("default"), codex_router.CodexAccount("two", "AH_CX_TOKEN_two")],
    )
    quota = codex_quota.CodexQuota(121, sentinel, balancer.QuotaWindow(30, 1000), balancer.QuotaWindow(50, 2000))
    monkeypatch.setattr(codex_router, "quotas", lambda pool, environ: {"two": quota})
    monkeypatch.setattr(daemon, "repositories", lambda: [str(tmp_path)])
    monkeypatch.setattr(daemon.time, "time", lambda: 122)
    report = daemon.observe()
    assert report["sessions"]["claude:one"] == 2
    assert report["sessions"]["codex:two"] == 3
    assert report["sessions"]["codex:default"] == 1
    assert report["interactive"]["codex"] == "default"
    assert report["repos"] == [str(tmp_path)]
    assert report["quota"]["claude:one"] == {
        "five_hour": {"used": 20, "resets_at": 1000},
        "seven_day": {"used": 40, "resets_at": 2000},
        "observed_at": 120.5,
    }
    assert report["quota"]["codex:two"] == {
        "five_hour": {"used": 30, "resets_at": 1000},
        "seven_day": {"used": 50, "resets_at": 2000},
        "observed_at": 121,
    }
    assert report["quota"]["codex:default"]["observed_at"] is None
    assert (
        next(slot for slot in report["slots"] if slot["harness"] == "codex" and slot["account"] == "default")["cap"]
        == 0
    )
    assert {(slot["harness"], slot["account"], slot["kind"]) for slot in report["slots"]} >= {
        ("claude", "one", "subscription"),
        ("codex", "two", "subscription"),
        ("codex", "default", "interactive"),
        ("claude", "api", "api"),
        ("codex", "api", "api"),
    }
    daemon.beat(redis, "machine")
    for key in redis.scan_iter():
        value = (
            redis.hgetall(key)
            if redis.type(key) == "hash"
            else (redis.smembers(key) if redis.type(key) == "set" else redis.get(key))
        )
        assert sentinel not in str(value)


def test_repositories_discover_checkouts_and_refresh_removals(monkeypatch, tmp_path):
    monkeypatch.setenv("HIVE_REPO_ROOT", str(tmp_path))
    repo = tmp_path / "org" / "project"
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    direct = tmp_path / "direct"
    direct.mkdir()
    (direct / ".git").write_text("gitdir: elsewhere")
    (tmp_path / "empty").mkdir()
    assert daemon.repositories() == sorted([str(direct), str(repo)])
    (repo / ".git").rmdir()
    assert daemon.repositories() == [str(direct)]


def test_cli_runs_the_daemon_and_installs_service(monkeypatch):
    from scripts.hive import cli

    connection = object()
    monkeypatch.setattr(cli, "redis_client", lambda: connection)
    run = Mock()
    install = Mock(return_value=True)
    monkeypatch.setattr(daemon, "run", run)
    monkeypatch.setattr(daemon, "install", install)
    monkeypatch.setattr(daemon, "clients", lambda source: ("bounded", source))
    assert cli.main(["run"]) == 0
    run.assert_called_once()
    assert run.call_args.args[0]() == ("bounded", connection)
    assert cli.main(["install"]) == 0
    install.assert_called_once_with()
    install.return_value = False
    assert cli.main(["install"]) == 1


def test_snapshot_quota_uses_the_newest_attributed_local_reading(monkeypatch, tmp_path):
    from hooks.context import broadcast, quota_usage
    from scripts import claude_quota_balancer as balancer

    monkeypatch.setattr(daemon.account_sessions, "live_sessions", lambda: {11: "one", 12: "one"})
    monkeypatch.setattr(
        broadcast,
        "_load_sessions",
        lambda: {
            "old": {"pid": 11, "account": "one"},
            "new": {"pid": 12, "account": "one"},
            "other": {"pid": 13, "account": "one"},
        },
    )
    monkeypatch.setattr(quota_usage, "_snapshot_path", lambda session: tmp_path / f"{session}.json")
    for session, at, used in (("old", 100, 10), ("new", 110, 20), ("other", 120, 90)):
        (tmp_path / f"{session}.json").write_text(
            json.dumps(
                {
                    "updated_at": at,
                    "rate_limits": {
                        "five_hour": {"used_percentage": used, "resets_at": 1000},
                        "seven_day": {"used_percentage": 30, "resets_at": 2000},
                    },
                }
            )
        )
    monkeypatch.setattr(daemon, "claude_signed_in", lambda: False)
    monkeypatch.setattr(daemon.codex_router, "accounts", lambda environ: [])
    monkeypatch.setattr(balancer, "cached_observations", lambda **kwargs: [])
    slots, quota = daemon._subscriptions({"AH_CC_TOKEN_one": "sentinel"}, {"claude": {}, "codex": {}}, 122)
    assert quota["claude:one"]["observed_at"] == 110
    assert quota["claude:one"]["five_hour"] == {"used": 20, "resets_at": 1000}
    assert quota["claude:one"]["seven_day"] == {"used": 30, "resets_at": 2000}


def test_idle_claude_login_is_advertised_without_credentials(monkeypatch):
    monkeypatch.setattr(
        daemon.subprocess,
        "run",
        Mock(
            return_value=Mock(
                returncode=0, stdout=json.dumps({"loggedIn": True, "authMethod": "claude.ai", "extra": "sentinel"})
            )
        ),
    )
    assert daemon.claude_signed_in() is True
    daemon.subprocess.run.assert_called_once_with(
        ["claude", "auth", "status", "--json"], capture_output=True, text=True, timeout=5
    )


@pytest.mark.parametrize(
    "result",
    [
        {"loggedIn": False, "authMethod": "claude.ai"},
        {"loggedIn": True, "authMethod": "api_key"},
    ],
)
def test_claude_status_requires_an_interactive_login(monkeypatch, result):
    monkeypatch.setattr(daemon.subprocess, "run", Mock(return_value=Mock(returncode=0, stdout=json.dumps(result))))
    assert daemon.claude_signed_in() is False


def test_missing_claude_binary_is_not_an_interactive_login(monkeypatch):
    monkeypatch.setattr(daemon.subprocess, "run", Mock(side_effect=FileNotFoundError))
    assert daemon.claude_signed_in() is False


def test_service_contract():
    assert daemon.unit("/bin/agentihooks") == (
        "[Unit]\nDescription=agentihooks hive heartbeat\n\n"
        "[Service]\nType=simple\n"
        "Environment=PATH=%h/.local/bin:%h/.cargo/bin:/usr/local/bin:/usr/bin:/bin\n"
        "EnvironmentFile=-%h/.agentihooks/.env\n"
        "EnvironmentFile=-%h/.agentihooks/hive.env\n"
        'ExecStart="/bin/agentihooks" hive run\n'
        "Restart=always\nRestartSec=5\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


def test_cli_help_describes_run_and_install():
    from scripts.hive import cli

    help_text = cli._parser().format_help()
    assert "  Publish hive telemetry every fifteen seconds\n" in help_text
    assert "  Write and enable the hive user service\n" in help_text


def test_initial_beat_creates_a_complete_registry_record(redis, monkeypatch):
    monkeypatch.setattr(
        daemon,
        "observe",
        lambda **kwargs: {
            "slots": [],
            "interactive": {"claude": "", "codex": ""},
            "repos": [],
            "sessions": {},
            "quota": {},
        },
    )
    daemon.beat(redis, "machine")
    assert registry.show(redis, "machine") == {
        "id": "machine",
        "name": "machine",
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
        "heartbeat_at": registry.show(redis, "machine")["heartbeat_at"],
        "version": "",
    }
    assert redis.smembers(registry.INDEX) == {"machine"}


def test_a_beat_moves_a_legacy_roles_record_and_seeds_its_own_key(redis, monkeypatch):
    monkeypatch.setattr(
        daemon,
        "observe",
        lambda **kwargs: {"slots": [], "interactive": {}, "repos": [], "sessions": {}, "quota": {}},
    )
    redis.hset(registry.key("machine"), mapping={"ui": "no", "roles": '["eng"]', "max_agents": "1"})
    daemon.beat(redis, "machine")
    stored = redis.hgetall(registry.key("machine"))
    assert "roles" not in stored and "max_agents" not in stored
    assert stored["name"] == "machine"
    assert "ephemeral" not in stored


def test_an_overlong_beat_does_not_sleep_again(monkeypatch):
    monkeypatch.setattr(daemon, "hive_id", lambda: "local")
    monkeypatch.setattr(daemon, "beat", lambda redis, hive: None)
    monkeypatch.setattr(daemon.quota_refresh, "refresh", lambda environ: None)
    monkeypatch.setattr(daemon.quota_refresh, "start", lambda environ, worker, attempted_at: (None, attempted_at))
    timestamps = iter([100, 116])
    monkeypatch.setattr(daemon.time, "monotonic", lambda: next(timestamps))
    sleep = Mock(side_effect=KeyboardInterrupt)
    monkeypatch.setattr(daemon, "scheduled_wait", sleep)
    connection = object()
    with pytest.raises(KeyboardInterrupt):
        daemon.run(_once(connection))
    sleep.assert_called_once_with(0, redis=connection, timeout=0)


def test_run_resumes_registration_after_a_dropped_and_restored_redis(monkeypatch):
    import fakeredis

    server = fakeredis.FakeServer()
    clients, sleeps, queued, now = [], [], [], [1000.0]
    stream = f"{registry.key('machine')}:orders"

    def connect():
        assert len(clients) < 3
        clients.append(fakeredis.FakeRedis(server=server, decode_responses=True))
        return clients[-1], clients[-1]

    def backoff(seconds):
        sleeps.append(seconds)
        server.connected = len(sleeps) == 2
        if server.connected:
            queued.append(clients[-1].xadd(stream, {"order": "{}"}))

    def wait(seconds, redis, timeout):
        assert redis is clients[-1]
        if now[0] == 2000.0:
            raise KeyboardInterrupt
        server.connected = False
        now[0] = 2000.0

    _quiet_quota(monkeypatch)
    monkeypatch.setattr(daemon, "hive_id", lambda: "machine")
    monkeypatch.setattr(
        daemon, "observe", lambda **kwargs: {"slots": [], "interactive": {}, "repos": [], "sessions": {}, "quota": {}}
    )
    monkeypatch.setattr(daemon.time, "time", lambda: now[0])
    monkeypatch.setattr(daemon.time, "sleep", backoff)
    monkeypatch.setattr(daemon, "scheduled_wait", wait)
    with pytest.raises(KeyboardInterrupt):
        daemon.run(connect)
    assert sleeps == [1, 2]
    assert len(clients) == 3
    assert registry.show(clients[-1], "machine")["heartbeat_at"] == 2000000
    assert clients[-1].xlen(stream) == 0
    assert "invalid_order" in clients[-1].hget(f"{stream}:acks", queued[0])


def test_run_backoff_is_capped_and_restarts_after_a_good_beat_and_wait(monkeypatch, capsys):
    from redis.exceptions import ConnectionError, TimeoutError

    outcomes = iter([ConnectionError] * 6 + [None, None, TimeoutError, None])
    connections, sleeps, waits = [], [], []

    def connect():
        assert len(connections) < 10
        connections.append(object())
        if len(connections) == 1:
            raise ConnectionError("refused")
        return connections[-1], connections[-1]

    def beat(redis, hive):
        assert (redis, hive) == (connections[-1], "local")
        error = next(outcomes)
        if error:
            raise error("lost")

    def wait(seconds, redis, timeout):
        assert redis is connections[-1]
        waits.append(seconds)
        if len(waits) == 1:
            raise ConnectionError("lost while waiting")
        if len(waits) == 3:
            raise KeyboardInterrupt

    _quiet_quota(monkeypatch)
    monkeypatch.setattr(daemon, "hive_id", lambda: "local")
    monkeypatch.setattr(daemon, "beat", beat)
    monkeypatch.setattr(daemon.time, "sleep", sleeps.append)
    monkeypatch.setattr(daemon, "scheduled_wait", wait)
    with pytest.raises(KeyboardInterrupt):
        daemon.run(connect)
    assert sleeps == [1, 2, 4, 8, 16, 30, 30, 30, 1]
    lines = capsys.readouterr().err.splitlines()
    assert lines[0] == "hive Redis connection lost (ConnectionError: refused); reconnecting in 1s"
    assert lines[2] == "hive Redis connection lost (ConnectionError: lost); reconnecting in 4s"
    assert len(connections) == 10


def test_run_lets_errors_other_than_a_lost_connection_escape(monkeypatch):
    _quiet_quota(monkeypatch)
    monkeypatch.setattr(daemon, "hive_id", lambda: "local")
    monkeypatch.setattr(daemon, "beat", Mock(side_effect=ValueError("bad report")))
    monkeypatch.setattr(daemon.time, "sleep", Mock(side_effect=AssertionError("retried")))
    with pytest.raises(ValueError, match="bad report"):
        daemon.run(_once(object()))


@pytest.fixture
def stalled():
    import socket

    import redis

    with socket.create_server(("127.0.0.1", 0)) as server:
        yield lambda: redis.Redis(host="127.0.0.1", port=server.getsockname()[1], decode_responses=True)


def test_clients_set_timeouts_keepalive_health_checks_and_no_retries(monkeypatch):
    import redis
    from redis.backoff import NoBackoff

    source = redis.Redis(host="127.0.0.1", port=1, decode_responses=True)
    monkeypatch.setattr(source, "close", Mock())
    beat, waiter = daemon.clients(source)
    source.close.assert_called_once_with()
    for client, read_timeout in ((beat, daemon.READ_TIMEOUT_S), (waiter, daemon.BEAT_SECONDS)):
        options = client.connection_pool.connection_kwargs
        assert options["socket_timeout"] == read_timeout
        assert options["socket_connect_timeout"] == daemon.CONNECT_TIMEOUT_S
        assert options["socket_keepalive"] is True
        assert options["health_check_interval"] == daemon.BEAT_SECONDS
        assert options["retry"].get_retries() == 0
        assert isinstance(options["retry"]._backoff, NoBackoff)
        assert options["decode_responses"] is True
        assert options["port"] == source.connection_pool.connection_kwargs["port"]
    assert (daemon.CONNECT_TIMEOUT_S, daemon.READ_TIMEOUT_S, daemon.WAKE_SLACK_S) == (3, 5, 2)


def test_back_off_logs_the_cause_and_sleeps_the_capped_delay(monkeypatch, capsys):
    from redis.exceptions import TimeoutError

    sleep = Mock()
    monkeypatch.setattr(daemon.time, "sleep", sleep)
    daemon._back_off(TimeoutError("lost"), 2)
    daemon._back_off(TimeoutError("lost"), 99)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "hive Redis connection lost (TimeoutError: lost); reconnecting in 4s\n"
        "hive Redis connection lost (TimeoutError: lost); reconnecting in 30s\n"
    )
    assert sleep.call_args_list == [((4,),), ((30,),)]


def test_clients_keep_the_source_connection_class():
    import redis

    source = redis.Redis.from_url("rediss://127.0.0.1:1")
    keys = set(source.connection_pool.connection_kwargs)
    for client in daemon.clients(source):
        assert client.connection_pool.connection_class is redis.SSLConnection
        assert client.connection_pool.connection_kwargs["socket_keepalive"] is True
        assert set(client.connection_pool.connection_kwargs) == keys | {
            "socket_timeout",
            "socket_connect_timeout",
            "socket_keepalive",
            "health_check_interval",
            "retry",
        }


def _stall_run(monkeypatch, connect, beat):
    import time

    monkeypatch.setattr(daemon, "BEAT_SECONDS", 0.5)
    monkeypatch.setattr(daemon, "READ_TIMEOUT_S", 0.1)
    monkeypatch.setattr(daemon, "WAKE_SLACK_S", 0.1)
    _quiet_quota(monkeypatch)
    monkeypatch.setattr(daemon, "hive_id", lambda: "local")
    monkeypatch.setattr(daemon, "beat", beat)
    monkeypatch.setattr(daemon.time, "sleep", Mock())
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        daemon.run(connect)
    return started


@pytest.mark.wall_clock
def test_a_stalled_beat_socket_reconnects_and_beats_within_one_beat_interval(monkeypatch, capsys, stalled):
    import time

    import fakeredis

    healthy = fakeredis.FakeRedis(decode_responses=True)
    clients, beaten_at = [], []

    def connect():
        return (healthy, healthy) if clients else daemon.clients(stalled())

    def beat(redis, hive):
        if not clients:
            assert redis.connection_pool.connection_kwargs["socket_timeout"] == 0.1
        clients.append(redis)
        redis.set("beat", hive)
        beaten_at.append(time.monotonic())

    monkeypatch.setattr(daemon, "scheduled_wait", Mock(side_effect=KeyboardInterrupt))
    started = _stall_run(monkeypatch, connect, beat)
    assert clients[1] is healthy
    assert beaten_at[0] - started < daemon.BEAT_SECONDS
    assert healthy.get("beat") == "local"
    err = capsys.readouterr().err
    assert err.startswith("hive Redis connection lost (")
    assert "TimeoutError" in err
    assert err.endswith("); reconnecting in 1s\n")
    daemon.time.sleep.assert_called_once_with(1)


@pytest.mark.wall_clock
def test_a_stalled_wait_socket_reconnects_and_beats_within_one_beat_interval(monkeypatch, capsys, stalled):
    import time

    import fakeredis

    healthy = fakeredis.FakeRedis(decode_responses=True)
    beats, waiters = [], []

    def connect():
        if waiters:
            return healthy, healthy
        waiters.append(daemon.clients(stalled())[1])
        return healthy, waiters[0]

    def beat(redis, hive):
        assert waiters[0].connection_pool.connection_kwargs["socket_timeout"] == 0.5
        beats.append(time.monotonic())
        if len(beats) == 2:
            raise KeyboardInterrupt

    from scripts.scheduler import process_timer

    monkeypatch.setattr(process_timer.service, "ensure_running", lambda: None)
    _stall_run(monkeypatch, connect, beat)
    assert beats[1] - beats[0] < daemon.BEAT_SECONDS + 0.25
    assert capsys.readouterr().err.endswith("); reconnecting in 1s\n")


def test_a_wait_that_never_wakes_returns_within_its_bound_and_beats_again(monkeypatch, capsys):
    import time
    from types import SimpleNamespace

    import fakeredis

    from scripts.scheduler import process_timer

    silent = fakeredis.FakeRedis(decode_responses=True)
    clock, beats, waits = [0.0], [], []

    def monotonic():
        return clock[0]

    def never_wakes(key, timeout):
        assert 0 < timeout <= process_timer.RECHECK_S
        waits.append(timeout)
        clock[0] += timeout
        return None

    def beat(redis, hive):
        beats.append(monotonic())
        if len(beats) == 2:
            raise KeyboardInterrupt

    def scheduler_down():
        assert monotonic() - beats[-1] < daemon.BEAT_SECONDS + 0.25, "the timer wait outlived its bound"

    monkeypatch.setattr(daemon, "time", SimpleNamespace(monotonic=monotonic, time=time.time, sleep=Mock()))
    monkeypatch.setattr(daemon, "publish_headroom", Mock())
    monkeypatch.setattr(process_timer, "time", SimpleNamespace(monotonic=monotonic))
    monkeypatch.setattr(silent, "blpop", never_wakes)
    monkeypatch.setattr(process_timer, "RECHECK_S", 0.05)
    monkeypatch.setattr(process_timer.service, "ensure_running", scheduler_down)
    connects = []
    _stall_run(monkeypatch, lambda: connects.append(silent) or (silent, silent), beat)
    assert len(connects) == 1
    assert waits
    assert beats[1] - beats[0] < daemon.BEAT_SECONDS
    assert capsys.readouterr().err == ""


def test_default_install_location_and_executable_discovery(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon.shutil, "which", lambda binary: "/bin/agentihooks" if binary == "agentihooks" else None)
    monkeypatch.setattr(daemon.Path, "home", lambda: tmp_path)
    run = Mock(return_value=Mock(returncode=0))
    assert daemon.install(run=run)
    assert (tmp_path / ".config/systemd/user/agentihooks-hive.service").read_text() == daemon.unit("/bin/agentihooks")
    assert run.call_args_list[0].kwargs == {"capture_output": True, "text": True, "timeout": 30, "check": True}
    assert run.call_args_list[1].kwargs == {"capture_output": True, "text": True, "timeout": 30}


def test_install_refuses_a_missing_executable(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon.shutil, "which", lambda binary: None)
    with pytest.raises(registry.HiveError) as caught:
        daemon.install(unit_dir=tmp_path)
    assert str(caught.value) == "agentihooks executable not found"


def test_install_reloads_a_changed_service(tmp_path):
    (tmp_path / "agentihooks-hive.service").write_text("old")
    run = Mock(return_value=Mock(returncode=0))
    assert daemon.install("/bin/agentihooks", tmp_path, run)
    assert run.call_count == 2
    assert (tmp_path / "agentihooks-hive.service").read_text() == daemon.unit("/bin/agentihooks")


def test_repositories_default_root_and_checkout_boundaries(monkeypatch, tmp_path):
    monkeypatch.delenv("HIVE_REPO_ROOT", raising=False)
    monkeypatch.setattr(daemon.Path, "home", lambda: tmp_path)
    assert daemon.repositories() == []
    root = tmp_path / "dev"
    root.mkdir()
    (root / "file").write_text("data")
    repo = root / "project"
    repo.mkdir()
    (repo / ".git").mkdir()
    nested = repo / "nested"
    nested.mkdir()
    (nested / ".git").mkdir()
    assert daemon.repositories() == [str(repo)]


@pytest.fixture
def observations(monkeypatch):
    from scripts import claude_quota_balancer as balancer

    monkeypatch.setattr(daemon, "claude_signed_in", lambda: False)
    monkeypatch.setattr(daemon, "_claude_snapshots", lambda observed: None)
    monkeypatch.setattr(balancer, "cached_observations", Mock(return_value=[]))
    monkeypatch.setattr(daemon.codex_router, "accounts", Mock(return_value=[]))
    monkeypatch.setattr(daemon.codex_router, "quotas", Mock(return_value={}))
    return balancer


def test_subscription_records_include_caps_unknown_readings_and_login(observations, monkeypatch):
    from scripts.codex_quota import CodexQuota

    balancer = observations
    result = balancer.ProbeResult(
        "one", "allowed", "OK", 60, balancer.QuotaWindow(65, 1000), balancer.QuotaWindow(40, 2000)
    )
    balancer.cached_observations.return_value = [(120, result)]
    account = daemon.codex_router.CodexAccount("two", "AH_CX_TOKEN_two")
    daemon.codex_router.accounts.return_value = [account, daemon.codex_router.CodexAccount("off", signed_in=False)]
    reading = CodexQuota(121, "", balancer.QuotaWindow(30, 1000), balancer.QuotaWindow(40, 2000))
    daemon.codex_router.quotas.return_value = {"two": reading}
    monkeypatch.setattr(daemon, "claude_signed_in", lambda: True)
    environ = {"AH_CC_TOKEN_one": "sentinel", "AH_CC_TOKEN_empty": "", "unrelated": "sentinel"}
    slots, quota = daemon._subscriptions(environ, {"claude": {"unobserved": 1, "api": 1}, "codex": {}}, 122)
    assert slots == [
        {"harness": "claude", "account": name, "kind": kind, "cap": cap}
        for name, kind, cap in sorted(
            [
                ("one", "subscription", 4),
                ("unobserved", "subscription", 0),
                (daemon.account_sessions.UNROUTED, "interactive", 0),
            ]
        )
    ] + [{"harness": "codex", "account": "two", "kind": "subscription", "cap": 6}]
    assert quota["claude:unobserved"] == {
        "five_hour": {"used": None, "resets_at": None},
        "seven_day": {"used": None, "resets_at": None},
        "observed_at": None,
    }
    balancer.cached_observations.assert_called_once_with(environ=environ)
    daemon.codex_router.accounts.assert_called_once_with(environ)
    daemon.codex_router.quotas.assert_called_once_with([account], environ)


def test_a_signed_out_codex_reading_publishes_no_quota_and_no_cap(observations):
    from scripts.codex_quota import CodexQuota

    balancer = observations
    balancer.cached_observations.return_value = []
    account = daemon.codex_router.CodexAccount("two", "AH_CX_TOKEN_two")
    daemon.codex_router.accounts.return_value = [account]
    reading = CodexQuota(121, "", balancer.QuotaWindow(30, 1000), balancer.QuotaWindow(40, 2000), expired=True)
    daemon.codex_router.quotas.return_value = {"two": reading}
    slots, quota = daemon._subscriptions({}, {"claude": {}, "codex": {}}, 122)
    assert slots == [{"harness": "codex", "account": "two", "kind": "subscription", "cap": 0}]
    assert quota["codex:two"] == {
        "five_hour": {"used": None, "resets_at": None},
        "seven_day": {"used": None, "resets_at": None},
        "observed_at": None,
    }


def test_stale_claude_reading_does_not_advertise_capacity(observations):
    result = observations.ProbeResult(
        "one", "allowed", "OK", 60, observations.QuotaWindow(20, 100000), observations.QuotaWindow(40, 200000)
    )
    observations.cached_observations.return_value = [(1, result)]
    slots, quota = daemon._subscriptions({"AH_CC_TOKEN_one": "sentinel"}, {"claude": {}, "codex": {}}, 10000)
    assert slots == [{"harness": "claude", "account": "one", "kind": "subscription", "cap": 0}]
    assert quota["claude:one"]["observed_at"] == 1


@pytest.mark.parametrize(
    ("environ", "high", "near"),
    [
        ({"AH_CC_TOKEN_high": "first", "AH_CC_TOKEN_near": "second"}, 11, 2),
        (
            {"AH_CC_TOKEN_high": "first", "AH_CC_TOKEN_near": "second", "AGENTIHOOKS_HIVE_SESSION_CEILING": "8"},
            7,
            2,
        ),
    ],
)
def test_hive_worker_caps_admit_more_at_high_quota_and_fewer_near_a_limit(
    observations, monkeypatch, environ, high, near
):
    monkeypatch.setattr(daemon.balancer.operator_env, "block_tokens", lambda: {})
    window = observations.QuotaWindow
    observations.cached_observations.return_value = [
        (120, observations.ProbeResult("high", "allowed", "OK", 60, window(4, 1000), window(2, 2000))),
        (120, observations.ProbeResult("near", "allowed", "OK", 60, window(92, 1000), window(2, 2000))),
    ]
    slots, _ = daemon._subscriptions(environ, {"claude": {}, "codex": {}}, 122)
    assert slots == [
        {"harness": "claude", "account": "high", "kind": "subscription", "cap": high},
        {"harness": "claude", "account": "near", "kind": "subscription", "cap": near},
    ]
    assert high > session_bands.TOP_BAND > near


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"AH_CC_TOKEN_one": "same", "AH_CC_TOKEN_two": "same"}, [("one", 4)]),
        ({"AH_CC_TOKEN_one": "first", "AH_CC_TOKEN_two": "second"}, [("one", 4), ("two", 0)]),
    ],
)
def test_token_names_holding_one_token_report_one_slot(observations, monkeypatch, environ, expected):
    monkeypatch.setattr(daemon.balancer.operator_env, "block_tokens", lambda: {})
    result = observations.ProbeResult(
        "one", "allowed", "OK", 60, observations.QuotaWindow(65, 1000), observations.QuotaWindow(40, 2000)
    )
    observations.cached_observations.return_value = [(120, result)]
    slots, quota = daemon._subscriptions(environ, {"claude": {}, "codex": {}}, 122)
    assert slots == [
        {"harness": "claude", "account": account, "kind": "subscription", "cap": cap} for account, cap in expected
    ]
    assert sorted(quota) == [f"claude:{account}" for account, _ in expected]


def test_snapshot_missing_windows_bad_records_and_newer_cache(monkeypatch, tmp_path):
    from hooks.context import quota_usage
    from scripts import claude_quota_balancer as balancer

    monkeypatch.setattr(daemon.account_sessions, "live_sessions", lambda: {1: "one"})
    rows = {
        "unrelated": {"pid": 2},
        "broken": {"pid": 1},
        "partial": {"pid": 1},
        "older": {"pid": 1},
        "same": {"pid": 1},
    }
    monkeypatch.setattr(daemon.broadcast, "_load_sessions", lambda: rows)
    monkeypatch.setattr(quota_usage, "_snapshot_path", lambda session: tmp_path / f"{session}.json")
    (tmp_path / "broken.json").write_text("bad json")
    for name, at, windows in (
        ("partial", 0.5, {}),
        ("older", 0.25, {"five_hour": {"used_percentage": 99, "resets_at": 8}}),
        ("same", 0.5, {"seven_day": {"used_percentage": 88, "resets_at": 9}}),
    ):
        (tmp_path / f"{name}.json").write_text(json.dumps({"updated_at": at, "rate_limits": windows}))
    observed = {}
    daemon._claude_snapshots(observed)
    at, result = observed["one"]
    assert at == 0.5
    assert result.five_hour == balancer.QuotaWindow()
    assert result.seven_day == balancer.QuotaWindow()


def test_api_fields_empty_login_and_zero_sessions(observations, monkeypatch):
    from scripts.routing.slots import Slot

    monkeypatch.setattr(daemon.account_sessions, "sessions_by_account", lambda **kwargs: {})
    monkeypatch.setattr(daemon.account_sessions, "codex_sessions_by_account", lambda **kwargs: {})
    monkeypatch.setattr(daemon, "repositories", lambda: [])
    monkeypatch.setattr(daemon.time, "time", lambda: 122)
    calls = []

    def slots(source, environ, now):
        calls.append((environ, now))
        return [Slot("claude", "api", 0, 0, kind="api", provider="anthropic")]

    monkeypatch.setattr(daemon.claude_api.ClaudeApiSource, "slots", slots)
    monkeypatch.setattr(daemon.codex_api.CodexApiSource, "slots", lambda *args: [])
    report = daemon.observe()
    assert report["interactive"] == {"claude": "", "codex": ""}
    assert report["sessions"] == {"claude:api": 0}
    assert report["slots"] == [
        {"harness": "claude", "account": "api", "kind": "api", "cap": 0, "provider": "anthropic"}
    ]
    assert calls == [(daemon.os.environ, 122)]


def test_observe_reports_host_counts_in_nonlocal_mode(observations, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_RUNTIME_BACKEND", "kubernetes")
    monkeypatch.setattr(
        daemon.account_sessions, "sessions_by_account", lambda *, local=False: {"one": 2 if local else 99}
    )
    monkeypatch.setattr(
        daemon.account_sessions,
        "codex_sessions_by_account",
        Mock(side_effect=lambda *, local=False: {} if local else {"two": 99}),
    )
    report = daemon.observe()
    assert report["sessions"]["claude:one"] == 2
    daemon.account_sessions.codex_sessions_by_account.assert_called_once_with(local=True)


def test_beat_keeps_a_launch_counted_between_process_samples(redis, monkeypatch):
    from scripts.routing import aggregate, reservation

    monkeypatch.setattr("time.time", lambda: 100)
    slots = reservation.LaunchSlots(redis)
    started = []

    def observe(**kwargs):
        assert slots.reserve("claude:one", "first", 1, 1000)
        assert slots.acknowledge("first")
        started.append("first")
        return {
            "slots": [{"harness": "claude", "account": "one", "kind": "subscription", "cap": 1}],
            "interactive": {},
            "repos": [],
            "quota": {},
            "sessions": {"claude:one": 0},
        }

    monkeypatch.setattr(daemon, "observe", observe)
    monkeypatch.setattr(
        daemon, "_executions", lambda redis, hive, processes: {"claude:one": list(started)} if started else {}
    )
    daemon.beat(redis, "hive")
    assert aggregate.read(redis, 100)["claude:one"]["sessions"] == 1
    assert not slots.reserve("claude:one", "second", 1, 1000)


@pytest.mark.parametrize("hive,host,remaining", [("", "hive", 0), ("hive", "cluster", 0), ("other", "hive", 1)])
def test_beat_exit_between_samples_cannot_create_negative_capacity(
    redis, observations, monkeypatch, hive, host, remaining
):
    from scripts.routing import aggregate, reservation
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
    from scripts.swarm_v2.controller import Controller
    from scripts.swarm_v2.registry import Scope, Session, encode

    monkeypatch.setattr("time.time", lambda: 100)
    store = RedisStore(redis)
    store.create(SwarmConfig("swarm", "repo", 1, 0))
    controller = Controller(store, "swarm", [], lambda: True)
    assert controller.acquire()
    agent = controller.admit(
        AgentRecord(
            store.next_name("swarm", "eng"),
            "eng",
            "task",
            hive=hive,
            seat="eng-1@swarm",
            harness="claude",
            account="one",
        ),
    )
    session = Session(
        "session",
        Scope("kubernetes", host, "worker"),
        4242,
        1,
        "claude",
        execution_id=agent.execution_id,
        generation=agent.generation,
    )
    redis.hset(store.key("swarm", "fleet-sessions"), session.key(), encode(session))
    monkeypatch.setattr(daemon.account_sessions, "local_processes", lambda: {"claude": {4242: "one"}, "codex": {}})
    slots = reservation.LaunchSlots(redis)
    assert slots.reserve("claude:one", agent.execution_id, 1, 1000)
    assert slots.acknowledge(agent.execution_id)
    original = daemon.observe

    def observe(**kwargs):
        assert slots.release(agent.execution_id)
        return original(**kwargs)

    monkeypatch.setattr(daemon, "observe", observe)
    daemon.beat(redis, "hive")
    assert aggregate.read(redis, 100)["claude:one"]["sessions"] == remaining
    assert slots.reserve("claude:one", "second", 1, 1000) is (remaining == 0)
    assert not slots.reserve("claude:one", "third", 1, 1000)


def test_observe_reports_interactive_counts_with_their_harness_and_account(observations, monkeypatch):
    from scripts.routing import reservation

    monkeypatch.setattr(
        daemon,
        "_subscriptions",
        lambda env, counts, now: ([{"harness": "codex", "account": "one", "kind": "subscription", "cap": 1}], {}),
    )
    monkeypatch.setattr(
        reservation, "interactive_sessions", lambda harness, account: 2 if (harness, account) == ("codex", "one") else 0
    )
    assert daemon.observe({"claude": {}, "codex": {1000: "one"}})["sessions"] == {
        "codex:one": 1,
        "interactive:codex:one": 2,
    }


def test_execution_report_deduplicates_processes_and_skips_stale_records(redis, monkeypatch):
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
    from scripts.swarm_v2.controller import Controller
    from scripts.swarm_v2.registry import CLOSED, Scope, Session, encode

    monkeypatch.setattr("time.time", lambda: 100)
    store = RedisStore(redis)
    store.create(SwarmConfig("swarm", "repo", 8, 0))
    controller = Controller(store, "swarm", [], lambda: True)
    assert controller.acquire()
    agents = [
        controller.admit(
            AgentRecord(
                store.next_name("swarm", "eng"),
                "eng",
                str(index),
                hive="hive",
                seat=f"eng-{index}@swarm",
                harness="claude",
                account="one",
            )
        )
        for index in range(7)
    ]
    orphan = Session(
        "orphan", Scope("kubernetes", "hive", "worker"), 70, 7, "claude", execution_id="exe-absent", generation=1
    )
    redis.hset(store.key("swarm", "fleet-sessions"), orphan.key(), encode(orphan))
    records = [
        (agents[0], 10, 1, "claude"),
        (agents[1], 20, 1, "claude"),
        (agents[2], 30, 1, "claude"),
        (agents[3], 30, 1, "claude"),
        (agents[4], 40, 2, "claude"),
        (agents[5], 50, 1, "unknown"),
        (agents[6], 60, 1, "claude"),
    ]
    for index, (agent, pid, generation, harness) in enumerate(records):
        session = Session(
            f"session-{index}",
            Scope("kubernetes", "hive", "worker"),
            pid,
            index,
            harness,
            execution_id=agent.execution_id,
            generation=generation,
            **({"state": CLOSED} if index == 1 else {}),
        )
        redis.hset(store.key("swarm", "fleet-sessions"), session.key(), encode(session))
    processes = {"claude": {10: "other", 20: "one", 30: "one", 40: "one", 60: "one"}, "codex": {}}
    assert daemon._executions(redis, "hive", processes) == {
        "claude:one": [agents[2].execution_id, agents[6].execution_id]
    }
    processes["claude"][10] = "one"
    assert daemon._executions(redis, "hive", processes) == {
        "claude:one": [agents[0].execution_id, agents[2].execution_id, agents[6].execution_id]
    }


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_beat_counts_a_named_live_execution_once_without_fleet_registration(redis, monkeypatch, harness):
    from hooks.proc import Process
    from scripts import terminate_agent
    from scripts.routing import aggregate, reservation
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    monkeypatch.setattr("time.time", lambda: 100)
    store = RedisStore(redis)
    store.create(SwarmConfig("swarm", "repo", 8, 0))
    agent = store.start_execution(
        "swarm",
        AgentRecord(
            store.next_name("swarm", "eng"),
            "eng",
            "task",
            hive="hive",
            harness=harness,
            account="one",
            seat="eng-1@swarm",
        ),
    )
    store.put_agent("swarm", agent)
    session = terminate_agent.Session(
        "session", harness, agent.name, Process(10, 1, 10, 10, 7, "S", harness, (harness,)), "", "alive"
    )
    monkeypatch.setattr(terminate_agent, "sessions", lambda: [session])
    monkeypatch.setattr(daemon.account_sessions, "local_processes", lambda: {harness: {10: "one"}})
    slot = f"{harness}:one"
    monkeypatch.setattr(
        daemon,
        "observe",
        lambda **kwargs: {
            "slots": [{"harness": harness, "account": "one", "kind": "subscription", "cap": 3}],
            "interactive": {},
            "repos": [],
            "quota": {},
            "sessions": {slot: 1},
        },
    )
    slots = reservation.LaunchSlots(redis)
    assert slots.reserve(slot, agent.execution_id, 3, 1000)
    assert slots.acknowledge(agent.execution_id)
    assert slots.reserve(slot, "unreported", 3, 1000)
    daemon.beat(redis, "hive")
    assert json.loads(redis.get(f"{registry.key('hive')}:sessions"))["_executions"] == {slot: [agent.execution_id]}
    assert aggregate.read(redis, 100)[slot]["sessions"] == 2
    assert slots.reserve(slot, "next", 3, 1000)
    assert not slots.reserve(slot, "full", 3, 1000)


@pytest.mark.parametrize(
    "mismatch", ["none", "hive", "harness", "account", "pid", "name", "host_id", "missing_harness", "duplicate"]
)
def test_named_execution_discovery_requires_a_local_matching_process(redis, monkeypatch, mismatch):
    from dataclasses import replace

    from hooks.proc import Process
    from scripts import terminate_agent
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

    store = RedisStore(redis)
    store.create(SwarmConfig("swarm", "repo", 8, 0))
    agents, sessions = [], []
    for index in range(2):
        agent = AgentRecord(
            store.next_name("swarm", "eng"),
            "eng",
            "task",
            hive="hive",
            harness="claude",
            account="one",
            seat=f"eng-{index + 1}@swarm",
        )
        if index == 0 and mismatch in {"hive", "harness", "account"}:
            agent = replace(agent, **{mismatch: "other"})
        if index == 0 and mismatch == "host_id":
            agent = replace(agent, hive="", runtime_target={"host_id": "hive"})
        agent = store.start_execution("swarm", agent)
        store.put_agent("swarm", agent)
        agents.append(agent)
        pid = 10 if mismatch == "duplicate" else 10 + index
        if index == 0 and mismatch == "pid":
            pid = 20
        sessions.append(
            terminate_agent.Session(
                "",
                "claude",
                "stray" if index == 0 and mismatch == "name" else agent.name,
                Process(pid, 1, pid, pid, 7, "S", "claude", ("claude",)),
                "",
                "unregistered",
            )
        )
    monkeypatch.setattr(terminate_agent, "sessions", lambda: sessions)
    processes = {} if mismatch == "missing_harness" else {"claude": {10: "one", 11: "one"}}
    expected = [agents[1].execution_id]
    if mismatch in {"none", "host_id"}:
        expected.insert(0, agents[0].execution_id)
    elif mismatch == "duplicate":
        expected = [agents[0].execution_id]
    elif mismatch == "missing_harness":
        expected = []
    assert daemon._executions(redis, "hive", processes) == ({"claude:one": expected} if expected else {})


def test_beat_publishes_what_this_hive_sees_of_its_own_executions(redis, monkeypatch):
    from dataclasses import replace

    from scripts.hive import execution_events, orders, reports
    from scripts.swarm.pane import PaneObservation
    from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
    from scripts.swarm_v2.runtime import process

    publisher = Mock()
    monkeypatch.setattr(execution_events, "observe", publisher)
    store = RedisStore(redis)
    store.create(SwarmConfig("swarm", "repo", 8, 0))
    here = AgentRecord(
        store.next_name("swarm", "eng"), "eng", "one", pane_id="w1:p1", runtime_target={"host_id": "machine"}
    )
    elsewhere = AgentRecord(
        store.next_name("swarm", "eng"), "eng", "two", pane_id="w1:p2", runtime_target={"host_id": "other"}
    )
    here = store.start_execution("swarm", replace(here, seat="eng-1@swarm"))
    store.put_agent("swarm", here)
    store.put_agent("swarm", store.start_execution("swarm", replace(elsewhere, seat="eng-2@swarm")))
    herdr = Mock()
    herdr.live_names.return_value = {here.name, "engineer@stray-0001"}
    herdr.bindings.return_value = {here.name: {"process": False}}
    herdr.conversations.return_value = {"w1:p1": "conversation"}
    herdr.herdr.return_value = {"panes": [{"pane_id": "w1:p1"}]}
    monkeypatch.setattr(process, "local_namespace", lambda: "hive-namespace")
    monkeypatch.setattr(daemon.time, "time", lambda: 123)
    herdr.observe.return_value = PaneObservation("idle", typed="half")
    herdr.reported.return_value = True
    consumer = Mock(runtime=Mock(herdr=herdr))
    monkeypatch.setattr(orders.Consumer, "local", lambda redis, hive: consumer)
    monkeypatch.setattr(
        daemon, "observe", lambda **kwargs: {"slots": [], "interactive": {}, "repos": [], "sessions": {}, "quota": {}}
    )
    daemon.beat(redis, "machine")
    publisher.assert_called_once_with("machine", json.loads(redis.get(reports.key("machine"))))
    assert [a.name for a in herdr.bindings.call_args.args[0]] == [here.name]
    assert herdr.reported.call_args.args[0].execution_id == here.execution_id
    assert herdr.observe.call_args.args[0].execution_id == here.execution_id
    assert json.loads(redis.get(reports.key("machine"))) == {
        "names": sorted({here.name, "engineer@stray-0001"}),
        "panes": {"w1:p1": "conversation"},
        "pane_ids": ["w1:p1"],
        "process_namespace": "hive-namespace",
        "observed_at": 123000,
        "executions": {
            here.execution_id: {
                "slug": "swarm",
                "name": here.name,
                "generation": here.generation,
                "reported": True,
                "binding": {"process": False},
                "pane": {
                    "state": "idle",
                    "prompt_title": "",
                    "typed": "half",
                    "provider_error": "",
                    "provider_checked": False,
                    "provider_error_id": "",
                    "content_refusal": False,
                },
            }
        },
    }
    assert redis.ttl(reports.key("machine")) == 45
    consumer.poll.assert_called_once_with()
    consumer.poll_commands.assert_called_once_with()


def test_a_hive_with_no_executions_reports_names_without_asking_herdr_more(redis, monkeypatch):
    from scripts.hive import orders, reports

    herdr = Mock()
    herdr.live_names.return_value = set()
    monkeypatch.setattr(orders.Consumer, "local", lambda redis, hive: Mock(runtime=Mock(herdr=herdr)))
    monkeypatch.setattr(
        daemon, "observe", lambda **kwargs: {"slots": [], "interactive": {}, "repos": [], "sessions": {}, "quota": {}}
    )
    daemon.beat(redis, "machine")
    assert json.loads(redis.get(reports.key("machine"))) == {"names": [], "panes": {}, "executions": {}}
    herdr.bindings.assert_not_called()
    herdr.conversations.assert_not_called()


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:The-Org/Project.git",
        "https://github.com/the-org/project",
        "ssh://git@github.com/the-org/project.git/",
    ],
)
def test_repositories_report_each_origin_as_a_lowercase_identity(monkeypatch, tmp_path, url):
    monkeypatch.setenv("HIVE_REPO_ROOT", str(tmp_path))
    repo = tmp_path / "project"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "config").write_text(f'[core]\n\tbare = false\n[remote "origin"]\n\turl = {url}\n')
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    (other / ".git" / "config").write_text('[remote "upstream"]\n\turl = git@github.com:else/where.git\n')
    assert daemon.repositories() == sorted(["github.com/the-org/project", str(other), str(repo)])


def test_repositories_follow_a_worktree_link_to_its_common_config(monkeypatch, tmp_path):
    monkeypatch.setenv("HIVE_REPO_ROOT", str(tmp_path / "root"))
    common = tmp_path / "main" / ".git"
    (common / "worktrees" / "wt").mkdir(parents=True)
    (common / "config").write_text('[remote "origin"]\n\turl = git@github.com:org/name.git\n')
    (common / "worktrees" / "wt" / "commondir").write_text("../..\n")
    checkout = tmp_path / "root" / "wt"
    checkout.mkdir(parents=True)
    (checkout / ".git").write_text(f"gitdir: {common / 'worktrees' / 'wt'}\n")
    assert daemon.repositories() == sorted(["github.com/org/name", str(checkout)])


def test_checkout_maps_an_identity_to_the_first_local_checkout_with_that_origin(monkeypatch, tmp_path):
    monkeypatch.setenv("HIVE_REPO_ROOT", str(tmp_path))
    origins = [
        ("b-copy", "git@github.com:Org/Name.git"),
        ("a-main", "https://github.com/org/name"),
        ("c", "git@github.com:org/other.git"),
    ]
    for name, url in origins:
        (tmp_path / name / ".git").mkdir(parents=True)
        (tmp_path / name / ".git" / "config").write_text(f'[remote "origin"]\n\turl = {url}\n')
    assert daemon.local_checkout("github.com/org/name") == str(tmp_path / "a-main")
    assert daemon.local_checkout("github.com/org/other") == str(tmp_path / "c")
    assert daemon.local_checkout("github.com/org/missing") == "github.com/org/missing"
    assert daemon.local_checkout(str(tmp_path / "b-copy")) == str(tmp_path / "b-copy")


def _reading(at):
    return {"five_hour": {"used": 10, "resets_at": 900}, "seven_day": {"used": 20, "resets_at": 990}, "observed_at": at}


def _publish(redis, monkeypatch, hive_id, slots, quota, beat_at):
    monkeypatch.setattr(
        daemon,
        "observe",
        lambda **kwargs: {"slots": slots, "interactive": {}, "repos": [], "sessions": {}, "quota": quota},
    )
    monkeypatch.setattr(daemon.time, "time", lambda: beat_at)
    daemon.beat(redis, hive_id)


def test_a_slot_without_a_newer_local_reading_takes_the_freshest_published_cap(redis, monkeypatch):
    def slot(account, cap, kind="subscription", harness="claude"):
        return {"harness": harness, "account": account, "kind": kind, "cap": cap}

    monkeypatch.setattr(daemon, "publish_config", lambda redis, local_id, repos: None)
    shared = ["one", "own", "same", "login"]
    _publish(
        redis,
        monkeypatch,
        "workstation",
        [slot(name, 6) for name in shared] + [slot("two", 4, harness="codex"), slot("blank", 3)],
        {f"claude:{name}": _reading(500) for name in shared}
        | {"codex:two": _reading(500), "claude:blank": _reading(None)},
        1000,
    )
    _publish(redis, monkeypatch, "older", [slot("one", 2)], {"claude:one": _reading(400)}, 1000)
    _publish(redis, monkeypatch, "edge", [slot("one", 7)], {"claude:one": _reading(600)}, 1000)
    _publish(redis, monkeypatch, "stale", [slot("one", 1)], {"claude:one": _reading(900)}, 1000)
    redis.hset(registry.key("edge"), "heartbeat_at", str(1_000_000 - registry.LIVE_MS))
    redis.hset(registry.key("stale"), "heartbeat_at", str(1_000_000 - registry.LIVE_MS - 1))
    local = [
        slot("one", 0),
        slot("two", 0, harness="codex"),
        slot("own", 3),
        slot("same", 5),
        slot("login", 0, kind="interactive"),
        slot("alone", 0),
        slot("blank", 0),
    ]
    quota = {
        "claude:one": _reading(None),
        "codex:two": _reading(None),
        "claude:own": _reading(600),
        "claude:same": _reading(500),
        "claude:login": _reading(None),
        "claude:alone": _reading(None),
        "claude:blank": _reading(None),
    }
    _publish(redis, monkeypatch, "pod", local, quota, 1000)
    caps = {(item["harness"], item["account"]): item["cap"] for item in registry.show(redis, "pod")["slots"]}
    assert caps == {
        ("claude", "one"): 7,
        ("codex", "two"): 4,
        ("claude", "own"): 3,
        ("claude", "same"): 5,
        ("claude", "login"): 0,
        ("claude", "alone"): 0,
        ("claude", "blank"): 0,
    }
    published = json.loads(redis.get(f"{registry.key('pod')}:quota"))
    assert published["claude:one"] == _reading(600)
    assert published["codex:two"] == _reading(500)
    assert published["claude:own"] == _reading(600)
    assert published["claude:alone"] == _reading(None)


def test_beat_publishes_the_configuration_with_the_repo_identities(redis, monkeypatch):
    calls = []
    monkeypatch.setattr(daemon, "publish_config", lambda redis, local_id, repos: calls.append((local_id, repos)))
    _publish(redis, monkeypatch, "pod", [], {}, 1000)
    monkeypatch.setattr(
        daemon,
        "observe",
        lambda **kwargs: {"slots": [], "interactive": {}, "repos": ["/x/a", "h/o/a"], "sessions": {}, "quota": {}},
    )
    daemon.beat(redis, "pod")
    assert calls == [("pod", []), ("pod", ["/x/a", "h/o/a"])]


def test_publish_config_hands_the_hive_id_and_repos_to_the_config_snapshot(redis, monkeypatch):
    from scripts.hive import config

    calls = []
    monkeypatch.setattr(config, "publish", lambda *args: calls.append(args))
    daemon.publish_config(redis, "pod", ["h/o/a"])
    assert calls == [(redis, "pod", ["h/o/a"])]


def test_existing_beat_refreshes_agent_join_heartbeats(redis, monkeypatch):
    from scripts.hive import join_report

    seen = []
    monkeypatch.setattr(join_report, "heartbeat", lambda *args: seen.append(args))
    _publish(redis, monkeypatch, "pod", [], {}, 1000)
    assert seen == [(redis, "pod", 1_000_000)]


def test_each_beat_closes_dead_tabs_against_the_reported_live_names(redis, monkeypatch):
    from scripts.hive import orders

    herdr = Mock()
    herdr.live_names.return_value = {"engineer@323133-2505"}
    monkeypatch.setattr(orders.Consumer, "local", lambda redis, hive: Mock(runtime=Mock(herdr=herdr)))
    monkeypatch.setattr(
        daemon, "observe", lambda **kwargs: {"slots": [], "interactive": {}, "repos": [], "sessions": {}, "quota": {}}
    )
    seen = []
    monkeypatch.setattr(daemon, "close_dead_tabs", lambda live: seen.append(live) or [])
    daemon.beat(redis, "machine")
    assert seen == [{"engineer@323133-2505"}]


def test_closing_dead_tabs_asks_the_local_herdr_with_the_beat_time(monkeypatch):
    import os

    from scripts import herdr_gc, herdr_host

    calls, checked = [], []
    monkeypatch.setattr(herdr_host, "binary", lambda: "/bin/herdr")
    monkeypatch.setattr(herdr_host, "server_running", lambda environ: checked.append(environ) or True)
    monkeypatch.setattr(daemon.time, "time", lambda: 123.456)
    monkeypatch.setattr(
        herdr_gc,
        "close_dead_tabs",
        lambda environ, now_ms, herdr, live: calls.append((environ, now_ms, herdr.environ, live)) or ["w1:t1"],
    )
    assert daemon.close_dead_tabs({"ci@323133-1356"}) == ["w1:t1"]
    assert checked == [dict(os.environ)]
    assert calls == [(dict(os.environ), 123456, dict(os.environ), {"ci@323133-1356"})]


@pytest.mark.parametrize("binary,running", [(None, True), ("/bin/herdr", False)])
def test_closing_dead_tabs_skips_a_hive_without_a_running_herdr(monkeypatch, binary, running):
    from scripts import herdr_gc, herdr_host

    monkeypatch.setattr(herdr_host, "binary", lambda: binary)
    monkeypatch.setattr(herdr_host, "server_running", lambda environ: running)
    monkeypatch.setattr(herdr_gc, "close_dead_tabs", Mock(side_effect=AssertionError("asked herdr")))
    assert daemon.close_dead_tabs(set()) == []


def test_a_herdr_read_error_leaves_the_beat_running(monkeypatch):
    from scripts import herdr_gc, herdr_host

    monkeypatch.setattr(herdr_host, "binary", lambda: "/bin/herdr")
    monkeypatch.setattr(herdr_host, "server_running", lambda environ: True)
    monkeypatch.setattr(herdr_gc, "close_dead_tabs", Mock(side_effect=herdr_gc.HerdrError("socket gone")))
    assert daemon.close_dead_tabs(set()) == []
