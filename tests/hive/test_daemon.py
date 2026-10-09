import json
from unittest.mock import Mock

import pytest

from scripts.hive import daemon, registry

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


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
    monkeypatch.setattr(daemon, "observe", lambda: report)
    monkeypatch.setattr(daemon.time, "time", lambda: 123.456)
    daemon.beat(redis, "machine")
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


def test_three_missed_beats_expire_both_keys(redis, monkeypatch):
    import time

    monkeypatch.setattr(
        daemon, "observe", lambda: {"slots": [], "interactive": {}, "repos": [], "sessions": {}, "quota": {}}
    )
    daemon.beat(redis, "machine")
    future = time.time() + 46
    with monkeypatch.context() as patcher:
        patcher.setattr(time, "time", lambda: future)
        assert redis.get(f"{registry.key('machine')}:sessions") is None
        assert redis.get(f"{registry.key('machine')}:quota") is None
    assert redis.exists(registry.key("machine"))


def test_run_beats_immediately_then_waits_fifteen_seconds(monkeypatch):
    events = []
    connection = object()
    monkeypatch.setattr(daemon, "beat", lambda redis, hive: events.append((redis, hive)))
    monkeypatch.setattr(daemon, "hive_id", lambda: "local")
    monkeypatch.setattr(daemon.time, "monotonic", lambda: 0)

    def sleep(seconds):
        events.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(daemon.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        daemon.run(connection)
    assert events == [(connection, "local"), 15]


def test_run_deducts_observation_time_from_the_beat_interval(monkeypatch):
    monkeypatch.setattr(daemon, "hive_id", lambda: "local")
    monkeypatch.setattr(daemon, "beat", lambda redis, hive: None)
    timestamps = iter([100, 103])
    monkeypatch.setattr(daemon.time, "monotonic", lambda: next(timestamps))
    sleep = Mock(side_effect=KeyboardInterrupt)
    monkeypatch.setattr(daemon.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        daemon.run(object())
    sleep.assert_called_once_with(12)


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
    monkeypatch.setattr(daemon.account_sessions, "sessions_by_account", lambda: {"one": 2})
    monkeypatch.setattr(daemon.account_sessions, "codex_sessions_by_account", lambda: {"two": 3, "default": 1})
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
    assert cli.main(["run"]) == 0
    run.assert_called_once_with(connection)
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
        lambda: {"slots": [], "interactive": {"claude": "", "codex": ""}, "repos": [], "sessions": {}, "quota": {}},
    )
    daemon.beat(redis, "machine")
    assert registry.show(redis, "machine") == {
        "id": "machine",
        "name": "machine",
        "ui": "no",
        "ephemeral": "no",
        "roles": [],
        "prefer": {},
        "max_agents": 1,
        "harnesses": [],
        "slots": [],
        "interactive": {"claude": "", "codex": ""},
        "repos": [],
        "heartbeat_at": registry.show(redis, "machine")["heartbeat_at"],
        "version": "",
    }
    assert redis.smembers(registry.INDEX) == {"machine"}


def test_an_overlong_beat_does_not_sleep_again(monkeypatch):
    monkeypatch.setattr(daemon, "hive_id", lambda: "local")
    monkeypatch.setattr(daemon, "beat", lambda redis, hive: None)
    timestamps = iter([100, 116])
    monkeypatch.setattr(daemon.time, "monotonic", lambda: next(timestamps))
    sleep = Mock(side_effect=KeyboardInterrupt)
    monkeypatch.setattr(daemon.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        daemon.run(object())
    sleep.assert_called_once_with(0)


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
                ("one", "subscription", 3),
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


def test_stale_claude_reading_does_not_advertise_capacity(observations):
    result = observations.ProbeResult(
        "one", "allowed", "OK", 60, observations.QuotaWindow(20, 100000), observations.QuotaWindow(40, 200000)
    )
    observations.cached_observations.return_value = [(1, result)]
    slots, quota = daemon._subscriptions({"AH_CC_TOKEN_one": "sentinel"}, {"claude": {}, "codex": {}}, 10000)
    assert slots == [{"harness": "claude", "account": "one", "kind": "subscription", "cap": 0}]
    assert quota["claude:one"]["observed_at"] == 1


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

    monkeypatch.setattr(daemon.account_sessions, "sessions_by_account", lambda: {})
    monkeypatch.setattr(daemon.account_sessions, "codex_sessions_by_account", lambda: {})
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
