import threading
from unittest.mock import Mock

import pytest

from scripts import claude_quota_balancer as balancer
from scripts import session_bands
from scripts.hive import daemon, quota_refresh

pytestmark = pytest.mark.unit

NOW = 2_000_000_000.0
ENVIRON = {"AH_CC_TOKEN_one": "sentinel", "AH_CC_TOKEN_two": "other", "unrelated": "x"}


def _result(account, used=20):
    return balancer.ProbeResult(
        account,
        "allowed",
        "OK",
        60,
        balancer.QuotaWindow(used, int(NOW) + 3600),
        balancer.QuotaWindow(used, int(NOW) + 86400),
    )


@pytest.fixture
def cached(monkeypatch):
    observations = Mock(return_value=[])
    monkeypatch.setattr(balancer, "cached_observations", observations)
    return observations


def test_a_hive_with_no_cache_is_due(cached):
    assert quota_refresh.due(ENVIRON, NOW) is True
    cached.assert_called_once_with(environ=ENVIRON)


def test_fresh_readings_for_every_account_are_not_due(cached):
    cached.return_value = [(NOW - 1, _result("one")), (NOW - 1, _result("two"))]
    assert quota_refresh.due(ENVIRON, NOW) is False


def test_one_missing_account_is_due(cached):
    cached.return_value = [(NOW - 1, _result("one"))]
    assert quota_refresh.due(ENVIRON, NOW) is True


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (session_bands.FRESH_SECONDS - quota_refresh.MARGIN_SECONDS, False),
        (session_bands.FRESH_SECONDS - quota_refresh.MARGIN_SECONDS + 1, True),
    ],
)
def test_a_reading_is_due_before_it_goes_stale(cached, age, expected):
    cached.return_value = [(NOW - age, _result("one")), (NOW, _result("two"))]
    assert quota_refresh.due(ENVIRON, NOW) is expected


def test_the_newest_reading_of_an_account_counts(cached):
    cached.return_value = [(NOW, _result("one")), (NOW - 10_000, _result("one")), (NOW, _result("two"))]
    assert quota_refresh.due(ENVIRON, NOW) is False


def test_a_hive_without_tokens_is_never_due(cached):
    assert quota_refresh.due({"unrelated": "x"}, NOW) is False
    cached.assert_not_called()


def test_refresh_probes_every_credential_when_due(cached, monkeypatch):
    collect = Mock(return_value=([], "live"))
    monkeypatch.setattr(balancer, "collect_results", collect)
    monkeypatch.setattr(quota_refresh.shutil, "which", lambda name: f"/bin/{name}")
    assert quota_refresh.refresh(ENVIRON, NOW) is True
    credentials = collect.call_args.args[0]
    assert sorted(credential.account for credential in credentials) == ["one", "two"]
    assert collect.call_args.kwargs == {"refresh": True, "environ": ENVIRON, "claude_bin": "/bin/claude", "now": NOW}


def test_refresh_falls_back_to_the_claude_name(cached, monkeypatch):
    collect = Mock(return_value=([], "live"))
    monkeypatch.setattr(balancer, "collect_results", collect)
    monkeypatch.setattr(quota_refresh.shutil, "which", lambda name: None)
    quota_refresh.refresh(ENVIRON, NOW)
    assert collect.call_args.kwargs["claude_bin"] == "claude"


def test_refresh_skips_the_probe_while_readings_are_fresh(cached, monkeypatch):
    cached.return_value = [(NOW, _result("one")), (NOW, _result("two"))]
    collect = Mock()
    monkeypatch.setattr(balancer, "collect_results", collect)
    assert quota_refresh.refresh(ENVIRON, NOW) is False
    collect.assert_not_called()


def test_refresh_probes_only_the_stale_accounts(cached, monkeypatch):
    cached.return_value = [(NOW, _result("one"))]
    collect = Mock(return_value=([], "live"))
    monkeypatch.setattr(balancer, "collect_results", collect)
    assert quota_refresh.refresh(ENVIRON, NOW) is True
    assert [credential.account for credential in collect.call_args.args[0]] == ["two"]


def test_start_runs_a_due_refresh_in_the_background(monkeypatch):
    calls, done = [], threading.Event()
    monkeypatch.setattr(quota_refresh, "due", lambda environ, now: True)

    def refresh(environ, now=None):
        calls.append((environ, now))
        done.set()

    monkeypatch.setattr(quota_refresh, "refresh", refresh)
    worker, attempted_at = quota_refresh.start(ENVIRON, None, NOW - quota_refresh.RETRY_SECONDS, NOW)
    assert worker.daemon is True
    assert attempted_at == NOW
    assert done.wait(5)
    worker.join(5)
    assert calls == [(ENVIRON, NOW)]


def test_start_keeps_a_running_refresh(monkeypatch):
    monkeypatch.setattr(quota_refresh, "due", lambda environ, now: True)
    running = Mock(is_alive=Mock(return_value=True))
    assert quota_refresh.start(ENVIRON, running, 0, NOW) == (running, 0)


@pytest.mark.parametrize("worker", [None, Mock(is_alive=Mock(return_value=False))])
def test_start_leaves_fresh_readings_alone(monkeypatch, worker):
    due = Mock(return_value=False)
    monkeypatch.setattr(quota_refresh, "due", due)
    assert quota_refresh.start(ENVIRON, worker, 0, NOW) == (worker, 0)
    due.assert_called_once_with(ENVIRON, NOW)


def test_start_waits_out_the_retry_window_after_an_attempt(monkeypatch):
    due = Mock(return_value=True)
    monkeypatch.setattr(quota_refresh, "due", due)
    attempted_at = NOW - quota_refresh.RETRY_SECONDS + 1
    assert quota_refresh.start(ENVIRON, None, attempted_at, NOW) == (None, attempted_at)
    due.assert_not_called()


def test_start_uses_the_clock_when_no_time_is_given(monkeypatch):
    monkeypatch.setattr(quota_refresh, "due", lambda environ, now: False)
    monkeypatch.setattr(quota_refresh.time, "time", lambda: NOW)
    assert quota_refresh.start(ENVIRON, None, NOW - 1) == (None, NOW - 1)


def test_a_hive_planted_without_a_cache_reports_a_nonzero_cap_after_start(monkeypatch, tmp_path):
    for name in [name for name in daemon.os.environ if name.startswith(balancer.TOKEN_PREFIX)]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("AH_CC_TOKEN_one", "sentinel")
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    environ = daemon.os.environ
    monkeypatch.setattr(daemon, "hive_id", lambda: "pod")
    monkeypatch.setattr(daemon, "claude_signed_in", lambda: False)
    monkeypatch.setattr(daemon, "_claude_snapshots", lambda observed: None)
    monkeypatch.setattr(daemon.codex_router, "accounts", lambda environ: [])
    monkeypatch.setattr(daemon.codex_router, "quotas", lambda pool, environ: {})
    monkeypatch.setattr(balancer, "_current_tokens", lambda environ: set())
    probe = Mock(side_effect=lambda missing, *args: [_result(c.account) for c in missing])
    monkeypatch.setattr(balancer, "probe_credentials", probe)
    monkeypatch.setattr(quota_refresh.time, "time", lambda: NOW)
    assert not (tmp_path / "claude-router-cache.json").exists()
    assert daemon._subscriptions(environ, {"claude": {}, "codex": {}}, NOW)[0][0]["cap"] == 0

    beats = []
    monkeypatch.setattr(
        daemon,
        "beat",
        lambda redis, local_id: beats.append(daemon._subscriptions(environ, {"claude": {}, "codex": {}}, NOW)[0]),
    )
    monkeypatch.setattr(daemon, "scheduled_wait", Mock(side_effect=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        daemon.run(lambda: (object(), object()))
    assert beats[0] == [{"harness": "claude", "account": "one", "kind": "subscription", "cap": 9}]
    probe.assert_called_once()
