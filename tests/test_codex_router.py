import base64
import json
import subprocess
import time

import pytest

from scripts import codex_router as router
from scripts.claude_quota_balancer import QuotaWindow
from scripts.codex_quota import CodexQuota
from scripts.routing import codex_api, place
from scripts.routing.slots import API_UNBOUNDED

ENV = {
    "HOME": "/home/u",
    "AH_CX_TOKEN_alpha": "cx-value-a",
    "AH_CX_TOKEN_beta": "cx-value-b",
    "AH_CC_TOKEN_ncgma": "cc-value",
    "CODEX_ACCESS_TOKEN": "stale-value",
}


NOW = 1_800_000_000


def _quota(used, observed_at=NOW):
    return CodexQuota(observed_at=observed_at, plan_type="team", seven_day=QuotaWindow(used=used))


def _status(returncode):
    def run(argv, **kwargs):
        assert argv[-2:] == ["login", "status"]
        assert "CODEX_ACCESS_TOKEN" not in kwargs["env"]
        assert not any(name.startswith("AH_CX_TOKEN_") for name in kwargs["env"])
        return subprocess.CompletedProcess(argv, returncode, "", "")

    return run


def _accounts(signed_in=True):
    return [
        router.CodexAccount("default", signed_in=signed_in),
        router.CodexAccount("alpha", "AH_CX_TOKEN_alpha"),
        router.CodexAccount("beta", "AH_CX_TOKEN_beta"),
    ]


def test_accounts_are_the_default_login_and_every_token_variable():
    assert router.accounts(ENV, run=_status(0)) == _accounts()
    assert router.accounts(ENV, run=_status(1))[0] == router.CodexAccount("default", signed_in=False)


def test_login_status_and_quota_read_the_profile_codex_home(tmp_path):
    operator = tmp_path / "operator"
    day = operator / "sessions" / "2026" / "10" / "06"
    day.mkdir(parents=True)
    event = '{"timestamp": "2026-10-06T10:00:00Z", "payload": {"rate_limits": {"primary": {"used_percent": 40, '
    event += '"window_minutes": 300}}}}\n'
    (day / f"rollout-x-{'a' * 36}.jsonl").write_text(event)
    profile = tmp_path / "rendered" / "engineer" / "codex"
    profile.mkdir(parents=True)
    (profile / "sessions").symlink_to(operator / "sessions")
    env = {**ENV, "CODEX_HOME": str(profile)}
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    assert router.default_signed_in(env, run=run)
    assert seen["CODEX_HOME"] == str(profile)
    assert router.child_environment(router.CodexAccount("default"), env)["CODEX_HOME"] == str(profile)
    quota = router.codex_quota.latest_codex_quota(env)
    assert quota.five_hour.used == 40.0


def test_the_account_with_the_fewest_sessions_under_its_week_band_wins():
    quotas = {"default": _quota(80.0), "alpha": _quota(30.0), "beta": _quota(10.0)}
    assert router.select(_accounts(), quotas, {}, NOW)[:2] == (_accounts()[1], "open")
    account, placement, seat = router.select(_accounts(), quotas, {"alpha": 1, "beta": 1}, NOW)
    assert (account, placement) == (_accounts()[0], "open")
    assert (seat.cap, seat.sessions) == (6, 0)


def test_equal_sessions_go_to_the_codex_account_whose_week_resets_soonest():
    soon = CodexQuota(NOW, "team", seven_day=QuotaWindow(used=50, resets_at=NOW + 600))
    late = CodexQuota(NOW, "team", seven_day=QuotaWindow(used=10, resets_at=NOW + 90000))
    spent = CodexQuota(NOW, "team", seven_day=QuotaWindow(used=98, resets_at=NOW + 60))
    quotas = {"default": late, "alpha": spent, "beta": soon}
    assert router.select(_accounts(), quotas, {}, NOW)[0] == _accounts()[2]
    tired = CodexQuota(NOW, "team", QuotaWindow(used=95), QuotaWindow(used=50, resets_at=NOW + 600))
    passed = CodexQuota(NOW, "team", seven_day=QuotaWindow(used=50, resets_at=NOW - 1))
    for other in (tired, passed):
        assert router.select(_accounts(), {**quotas, "beta": other}, {}, NOW)[0] == _accounts()[0]
    rested = CodexQuota(NOW, "team", QuotaWindow(used=97, resets_at=NOW - 1), soon.seven_day)
    assert router.select(_accounts(), {**quotas, "beta": rested}, {}, NOW)[0] == _accounts()[2]
    assert router.select(_accounts(), quotas, {"beta": 1}, NOW)[0] == _accounts()[0]
    assert [seat.spend_before for seat in router.seats(_accounts(), quotas, {}, NOW)] == [
        NOW + 90000,
        NOW + 60,
        NOW + 600,
    ]


def test_a_week_under_five_percent_or_a_full_band_takes_no_session():
    quotas = {"default": _quota(96.0), "alpha": _quota(20.0), "beta": _quota(30.0)}
    assert router.select(_accounts(), quotas, {"alpha": 6}, NOW)[0] == _accounts()[2]
    with pytest.raises(router.RoutingError, match="free session under its quota band"):
        router.select(_accounts(), quotas, {"alpha": 6, "beta": 6}, NOW)


def test_a_signed_out_missing_or_stale_reading_is_never_picked():
    quotas = {"default": _quota(10.0), "alpha": _quota(10.0, observed_at=NOW - 901), "beta": None}
    with pytest.raises(router.RoutingError):
        router.select(_accounts(signed_in=False), quotas, {}, NOW)
    quotas["alpha"] = _quota(10.0, observed_at=NOW - 900)
    assert router.select(_accounts(signed_in=False), quotas, {}, NOW)[0] == _accounts()[1]


def test_the_cap_comes_from_the_week_alone_and_a_passed_reset_reads_full():
    spent = CodexQuota(NOW, "team", QuotaWindow(used=99), QuotaWindow(used=97, resets_at=NOW + 600))
    assert router.account_cap(spent, NOW) == 0
    assert router.account_cap(spent, NOW + 600) == 6
    assert router.account_cap(_quota(95.0), NOW) == 6
    assert router.account_cap(None, NOW) is None


def test_a_forced_route_ignores_quota_and_cap():
    quotas = {"default": _quota(1.0), "alpha": _quota(99.0), "beta": _quota(1.0)}
    assert router.select(_accounts(), quotas, {"alpha": 9}, NOW, route="alpha") == (_accounts()[1], "forced", None)
    with pytest.raises(router.RoutingError, match="available: default, alpha, beta"):
        router.select(_accounts(), quotas, {}, NOW, route="gamma")


def test_no_seat_names_why_and_seats_are_codex():
    with pytest.raises(
        router.RoutingError,
        match=r"^no signed in Codex account has a fresh reading and a free session under its quota band$",
    ):
        router.select(_accounts(), {}, {}, NOW)
    assert {seat.harness for seat in router.seats(_accounts(), {"default": _quota(1.0)}, {}, NOW)} == {"codex"}


def test_route_reads_quotas_for_a_forced_account_and_refreshes_them_otherwise(monkeypatch):
    calls = []
    pool = _accounts()
    runner = object()
    monkeypatch.setattr(router, "codex_sessions_by_account", lambda: {})
    monkeypatch.setattr(router, "routing_pool", lambda environ, run: pool)
    monkeypatch.setattr(router, "quotas", lambda p, environ: calls.append(("quotas", p, environ)) or {})
    monkeypatch.setattr(
        router,
        "fresh_quotas",
        lambda p, environ, now, run: calls.append(("fresh", p, environ, run)) or {"default": _quota(1.0, time.time())},
    )
    assert router._route(ENV, "alpha", runner) == (pool[1], "forced", 0, "?")
    assert router._route(ENV, "", runner) == (pool[0], "open", 0, "6")
    assert calls == [("quotas", pool, ENV), ("fresh", pool, ENV, runner)]


def test_a_stale_reading_is_refreshed_by_a_probe_before_placing(monkeypatch):
    probed = []
    fresh = _quota(40.0)
    monkeypatch.setattr(router, "quotas", lambda pool, environ: {"default": _quota(10.0, observed_at=NOW - 901)})
    monkeypatch.setattr(router, "probe", lambda account, environ, run: probed.append(account.name) or fresh)
    pool = [router.CodexAccount("default"), router.CodexAccount("alpha", "AH_CX_TOKEN_alpha", signed_in=False)]
    assert router.fresh_quotas(pool, ENV, NOW) == {"default": fresh}
    assert probed == ["default"]
    monkeypatch.setattr(router, "probe", lambda account, environ, run: None)
    assert router.fresh_quotas(pool, ENV, NOW)["default"].observed_at == NOW - 901


def test_a_failed_probe_waits_a_freshness_window_before_the_next(monkeypatch):
    probed = []
    monkeypatch.setattr(router, "quotas", lambda pool, environ: {"default": None})
    monkeypatch.setattr(router, "probe", lambda account, environ, run: probed.append(account.name))
    pool = [router.CodexAccount("default")]
    router.fresh_quotas(pool, ENV, NOW)
    router.fresh_quotas(pool, ENV, NOW + 900)
    assert probed == ["default"]
    router.fresh_quotas(pool, ENV, NOW + 901)
    assert probed == ["default", "default"]


def test_fresh_quotas_hands_its_pool_environment_and_runner_on_and_skips_fresh_readings(monkeypatch):
    calls = []
    fresh, stale = _quota(10.0), _quota(10.0, observed_at=NOW - 901)
    monkeypatch.setattr(
        router, "quotas", lambda pool, environ: calls.append((pool, environ)) or {"default": fresh, "alpha": stale}
    )
    monkeypatch.setattr(router, "probe", lambda account, environ, run: calls.append((account.name, environ, run)))
    pool = _accounts()[:2]
    runner = object()
    assert router.fresh_quotas(pool, ENV, NOW, runner) == {"default": fresh, "alpha": stale}
    assert calls == [(pool, ENV), ("alpha", ENV, runner)]


def test_probe_attempts_live_in_one_file_and_survive_bad_contents(monkeypatch):
    from pathlib import Path

    path = Path.home() / ".agentihooks" / "codex-probe-attempts.json"
    assert router._attempts_path() == path
    path.write_text("not json")
    assert router._probe_attempts() == {}
    path.unlink()
    path.mkdir()
    monkeypatch.setattr(router, "quotas", lambda pool, environ: {"default": None})
    monkeypatch.setattr(router, "probe", lambda account, environ, run: None)
    assert router.fresh_quotas([router.CodexAccount("default")], ENV, NOW) == {"default": None}


def test_the_probe_runs_one_tiny_exec_on_the_account_and_reads_its_rollout(monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen.update(argv=argv, kwargs=kwargs)
        out = 'not json\n[1]\n{"type": "turn.started", "thread_id": "t-0"}\n'
        out += '{"type": "thread.started", "thread_id": "t-1"}\n{"type": "turn.completed"}\n'
        return subprocess.CompletedProcess(argv, 0, out, "")

    def session_quota(environ, thread):
        seen.update(environ=environ)
        return thread

    monkeypatch.setattr(router.shutil, "which", lambda name: {"codex": "/usr/bin/codex"}.get(name))
    monkeypatch.setattr(router.codex_quota, "session_quota", session_quota)
    assert router.probe(_accounts()[1], ENV, run) == "t-1"
    assert seen["argv"] == ["/usr/bin/codex", "--no-daemon", *router.PROBE_ARGS]
    assert seen["kwargs"] == {
        "env": router.child_environment(_accounts()[1], ENV),
        "capture_output": True,
        "text": True,
        "stdin": subprocess.DEVNULL,
        "timeout": router.PROBE_TIMEOUT_S,
    }
    assert seen["environ"] == ENV
    monkeypatch.setattr(router.shutil, "which", lambda name: None)
    no_start = '{"type": "turn.completed"}\n'
    assert (
        router.probe(_accounts()[1], ENV, lambda argv, **kw: subprocess.CompletedProcess(argv, 0, no_start, "")) is None
    )
    no_thread = '{"type": "thread.started"}\n'
    assert (
        router.probe(_accounts()[1], ENV, lambda argv, **kw: subprocess.CompletedProcess(argv, 0, no_thread, ""))
        is None
    )
    assert router.probe(_accounts()[1], ENV, lambda argv, **kw: subprocess.CompletedProcess(argv, 0, None, "")) is None
    assert router.probe(_accounts()[1], ENV, lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "")) is None

    def stuck(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    assert router.probe(_accounts()[1], ENV, stuck) is None


def test_a_session_quota_is_read_from_the_rollout_named_by_its_session(tmp_path):
    day = tmp_path / "sessions" / "2026" / "10" / "08"
    day.mkdir(parents=True)
    event = '{"timestamp": "2026-10-08T10:00:00Z", "payload": {"rate_limits": {"secondary": {"used_percent": 74, '
    event += '"window_minutes": 10080}}}}\n'
    (day / "rollout-2026-10-08T10-00-00-t-1.jsonl").write_text(event)
    env = {"CODEX_HOME": str(tmp_path)}
    assert router.codex_quota.session_quota(env, "t-1").seven_day.used == 74.0
    assert router.codex_quota.session_quota(env, "t-2") is None


def test_a_token_child_keeps_only_its_token_and_runs_without_the_shared_daemon():
    child = router.child_environment(_accounts()[1], ENV)
    assert child["CODEX_ACCESS_TOKEN"] == "cx-value-a"
    assert child["AH_CX_TOKEN_alpha"] == "cx-value-a"
    assert "AH_CX_TOKEN_beta" not in child and "AH_CC_TOKEN_ncgma" not in child
    assert router.command(_accounts()[1], "/usr/bin/codex", ["-m", "o3"]) == [
        "/usr/bin/codex",
        "--no-daemon",
        "-m",
        "o3",
    ]


def test_the_default_child_runs_on_the_stored_login_with_no_token():
    child = router.child_environment(_accounts()[0], ENV)
    assert not any(name.startswith(("AH_CX_TOKEN_", "AH_CC_TOKEN_", "CODEX_ACCESS_TOKEN")) for name in child)
    assert child["HOME"] == "/home/u"
    assert router.command(_accounts()[0], "/usr/bin/codex", ["-m", "o3"]) == ["/usr/bin/codex", "-m", "o3"]


def _launch(monkeypatch, tmp_path, environ, argv, quotas=None, sessions=None):
    seen = {}
    monkeypatch.setattr(router, "codex_sessions_by_account", lambda: sessions or {})
    monkeypatch.setattr(router, "quotas", lambda accounts, environ: quotas or {})
    monkeypatch.setattr(router, "probe", lambda account, environ, run: None)
    monkeypatch.setattr(router.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(router.time, "time", lambda: NOW)

    def execvpe(path, cmd, env):
        seen.update(path=path, cmd=cmd, env=env)

    report = tmp_path / "launch.route"
    rc = router.main(
        ["--agentihooks-report", str(report), *argv],
        environ={**environ, "HOME": str(tmp_path)},
        execvpe=execvpe,
        run=_status(0),
    )
    return rc, seen, report


def test_a_routed_launch_reports_the_account_and_never_prints_a_token(monkeypatch, tmp_path, capsys):
    quotas = {"default": _quota(90.0), "alpha": _quota(10.0), "beta": _quota(50.0)}
    rc, seen, report = _launch(monkeypatch, tmp_path, ENV, ["-m", "o3"], quotas)
    assert rc == 0
    assert seen["cmd"] == ["/usr/bin/codex", "--no-daemon", "-m", "o3"]
    assert seen["env"]["CODEX_ACCESS_TOKEN"] == "cx-value-a"
    assert report.read_text() == "status=routed\naccount=alpha\nplacement=open\n"
    out = capsys.readouterr()
    assert "account=alpha sessions=0/6 placement=open" in out.out
    assert "cx-value" not in out.out + out.err


def test_route_forces_an_account(monkeypatch, tmp_path):
    rc, seen, report = _launch(monkeypatch, tmp_path, ENV, ["--route", "beta", "-m", "o3"])
    assert rc == 0 and seen["env"]["CODEX_ACCESS_TOKEN"] == "cx-value-b"
    assert seen["cmd"] == ["/usr/bin/codex", "--no-daemon", "-m", "o3"]
    assert "account=beta\nplacement=forced" in report.read_text()


def test_no_routable_account_fails_the_launch_and_reports_it(monkeypatch, tmp_path, capsys):
    quotas = {"default": _quota(99.0), "alpha": _quota(99.0), "beta": _quota(99.0)}
    rc, seen, report = _launch(monkeypatch, tmp_path, ENV, [], quotas)
    assert rc == 3 and not seen
    assert report.read_text().startswith("status=failed\n")
    assert "cx-value" not in capsys.readouterr().err


def test_without_token_variables_the_default_login_is_judged_by_its_week(monkeypatch, tmp_path):
    def no_status(argv, **kwargs):
        raise AssertionError("no login status check without tokens")

    environ = {"AH_CC_TOKEN_ncgma": "cc-value"}
    rc, seen, report = _launch(monkeypatch, tmp_path, environ, ["-m", "o3", "do it"], {"default": _quota(50.0)})
    assert rc == 0
    assert seen["cmd"] == ["/usr/bin/codex", "-m", "o3", "do it"]
    assert "CODEX_ACCESS_TOKEN" not in seen["env"]
    assert report.read_text() == "status=routed\naccount=default\nplacement=open\n"
    rc, seen, report = _launch(monkeypatch, tmp_path, environ, [], {"default": _quota(50.0)}, {"default": 6})
    assert rc == 3 and not seen
    monkeypatch.setattr(router, "codex_sessions_by_account", lambda: {"default": 5})
    assert router._route(environ, "", no_status) == (router.CodexAccount("default"), "open", 5, "6")


def test_the_api_route_is_forced_with_its_own_live_count_and_no_cap(monkeypatch):
    def no_status(argv, **kwargs):
        raise AssertionError("no login status check on the api route")

    environ = dict.fromkeys(["CODEX_API_KEY"], "1")
    monkeypatch.setattr(router, "codex_sessions_by_account", lambda: {"api": 2, "default": 5})
    expected = router.CodexAccount("api", key_env="CODEX_API_KEY")
    assert router._route(environ, "api", no_status) == (expected, "forced", 2, "?")
    monkeypatch.setattr(router, "codex_sessions_by_account", lambda: {"default": 5})
    assert router._route(environ, "api", no_status) == (expected, "forced", 0, "?")


def _weighted(monkeypatch, weight, cap=API_UNBOUNDED):
    def policy(harness, environ):
        assert harness == "codex"
        return place.ApiPolicy(weight, cap)

    monkeypatch.setattr(place, "policy", policy)


def test_the_codex_api_share_counts_live_sessions_on_accounts_without_a_seat(monkeypatch):
    _weighted(monkeypatch, 25)
    quotas = {"default": _quota(10.0), "alpha": _quota(10.0)}
    assert router.select(_accounts(), quotas, {"beta": 4, "api": 1}, NOW, environ=API_ENV)[0] == API
    assert router.select(_accounts(), quotas, {"beta": 2, "api": 1}, NOW, environ=API_ENV)[0].name == "alpha"


def test_the_codex_launch_time_reaches_the_api_side(monkeypatch):
    seen = []

    def api_side(source, harness, environ, now):
        seen.append(now)
        return [], 0

    monkeypatch.setattr(place, "api_side", api_side)
    router.select(_accounts(), {"alpha": _quota(10.0)}, {}, NOW, environ=API_ENV)
    assert seen == [NOW]


def test_an_unreadable_codex_routing_setting_closes_only_the_api_side(monkeypatch, capsys):
    def unreadable(harness, environ):
        raise place.SettingsError("routing settings are unreadable: KeyError")

    monkeypatch.setattr(place, "policy", unreadable)
    assert router.select(_accounts(), {"alpha": _quota(10.0)}, {}, NOW, environ=API_ENV)[0].name == "alpha"
    assert capsys.readouterr().err == "[codex] api side closed: routing settings are unreadable: KeyError\n"
    with pytest.raises(router.RoutingError):
        router.select(_accounts(), {}, {}, NOW, environ=API_ENV)


API_ENV = {**ENV, "CODEX_API_KEY": "key"}
API = router.CodexAccount("api", key_env="CODEX_API_KEY")


def test_three_accounts_and_an_api_at_weight_25_give_api_one_of_every_four_sessions(monkeypatch):
    _weighted(monkeypatch, 25)
    quotas = {"default": _quota(10.0), "alpha": _quota(10.0), "beta": _quota(10.0)}
    sessions, chosen = {}, []
    for _ in range(8):
        account, placement, seat = router.select(_accounts(), quotas, dict(sessions), NOW, environ=API_ENV)
        chosen.append(account.name)
        assert (placement, seat.account) == ("open", account.name)
        sessions[account.name] = sessions.get(account.name, 0) + 1
    assert chosen == ["api", "alpha", "beta", "default"] * 2


def test_with_every_codex_week_below_five_percent_every_session_goes_to_api(monkeypatch):
    _weighted(monkeypatch, 0)
    quotas = {"default": _quota(97.0), "alpha": _quota(97.0), "beta": _quota(99.0)}
    for live in range(3):
        account, placement, seat = router.select(_accounts(), quotas, {"api": live}, NOW, environ=API_ENV)
        assert (account, placement, seat.cap, seat.sessions, seat.kind) == (API, "open", API_UNBOUNDED, live, "api")


def test_codex_routing_fails_only_when_the_pool_and_the_api_are_both_closed(monkeypatch):
    _weighted(monkeypatch, 0, cap=1)
    quotas = {"default": _quota(97.0), "alpha": _quota(97.0), "beta": _quota(99.0)}
    assert router.select(_accounts(), quotas, {}, NOW, environ=API_ENV)[0] == API
    with pytest.raises(router.RoutingError) as raised:
        router.select(_accounts(), quotas, {"api": 1}, NOW, environ=API_ENV)
    assert str(raised.value) == (
        "no signed in Codex account has a fresh reading and a free session under its quota band"
    )


def test_without_api_variables_no_routing_setting_is_read(monkeypatch):
    monkeypatch.setattr(place, "policy", lambda harness, environ: pytest.fail("policy read without an api slot"))
    quotas = {"default": _quota(90.0), "alpha": _quota(10.0), "beta": _quota(50.0)}
    assert router.select(_accounts(), quotas, {}, NOW, environ=ENV)[0].name == "alpha"
    assert router.select(_accounts(), quotas, {}, NOW)[0].name == "alpha"


def test_an_open_route_to_the_api_launches_with_the_api_scrubber(monkeypatch, tmp_path, capsys):
    _weighted(monkeypatch, 100)
    quotas = {"default": _quota(10.0), "alpha": _quota(10.0), "beta": _quota(10.0)}
    rc, seen, report = _launch(monkeypatch, tmp_path, API_ENV, ["-m", "o3"], quotas)
    assert rc == 0
    assert seen["cmd"] == ["/usr/bin/codex", "--no-daemon", *codex_api.overrides("CODEX_API_KEY", ""), "-m", "o3"]
    assert seen["env"]["AH_ROUTE_API"] == "1"
    assert seen["env"]["CODEX_API_KEY"] == "key"
    assert not [name for name in seen["env"] if name.startswith("AH_CX_TOKEN_") or name == "CODEX_ACCESS_TOKEN"]
    assert report.read_text() == "status=routed\naccount=api\nplacement=open\n"
    assert "account=api sessions=0/1000000 placement=open" in capsys.readouterr().out


def _jwt(claims, payload=None):
    def part(data):
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    body = payload if payload is not None else json.dumps(claims).encode()
    return ".".join([part(b'{"alg":"RS256"}'), part(body), "c2ln"])


OAUTH = _jwt({"iss": "https://auth.openai.com", "https://api.openai.com/auth": {"chatgpt_account_id": "acct-1"}})
OAUTH_TWO = _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct-2", "chatgpt_plan_type": "pro"}})
AGENT = _jwt({"iss": "https://chatgpt.com/codex-backend/agent-identity", "agent_runtime_id": "rt-1", "account_id": "a"})
AGENT_WITH_AUTH = _jwt({"agent_runtime_id": "rt-2", "https://api.openai.com/auth": {"chatgpt_account_id": "acct-3"}})
OAUTH_ENV = {
    "HOME": "/home/u",
    "AH_CX_TOKEN_alpha": OAUTH,
    "AH_CX_TOKEN_beta": OAUTH_TWO,
    "AH_CX_TOKEN_gamma": AGENT,
    "AH_CX_TOKEN_delta": "at-personal",
    "AH_CX_TOKEN_keyed": "sk-proj-value",
    "AH_CX_TOKEN_bare": _jwt({"https://api.openai.com/auth": {"chatgpt_plan_type": "pro"}}),
    "AGENTIHOOKS_CHATGPT_ACCOUNT_ID": "inherited",
    "CODEX_ACCESS_TOKEN": "stale",
}


def _token(name):
    return router.CodexAccount(name, f"AH_CX_TOKEN_{name}")


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        (OAUTH, "chatgpt-oauth"),
        (OAUTH_TWO, "chatgpt-oauth"),
        (AGENT, "codex-access-token"),
        (AGENT_WITH_AUTH, "codex-access-token"),
        ("at-personal", "codex-access-token"),
        ("cx-value-a", "codex-access-token"),
        ("a.b.c", "codex-access-token"),
        (_jwt(None, b"[1, 2]"), "codex-access-token"),
        (_jwt(None, b"\xff\xfe"), "codex-access-token"),
        (_jwt({"sub": "user"}), "codex-access-token"),
        (_jwt({"https://api.openai.com/auth": "acct-1"}) + ".extra", "codex-access-token"),
    ],
)
def test_the_selected_token_alone_is_classified_by_its_claims(value, kind):
    assert router.credential_kind(_token("alpha"), {"AH_CX_TOKEN_alpha": value}) == kind


def test_the_default_login_and_the_api_account_need_no_token_to_classify():
    assert router.credential_kind(router.CodexAccount("default"), {}) == "default"
    api = router.CodexAccount("api", key_env="CODEX_API_KEY")
    assert router.credential_kind(api, {"AH_CX_TOKEN_api": OAUTH}) == "api"
    assert router.credential(api, {}) == api
    assert router.credential(router.CodexAccount("default"), OAUTH_ENV) == router.CodexAccount("default")


@pytest.mark.parametrize(
    ("name", "environ", "error"),
    [
        ("ghost", OAUTH_ENV, "Codex account 'ghost' has no token in AH_CX_TOKEN_ghost"),
        ("alpha", {"AH_CX_TOKEN_alpha": ""}, "Codex account 'alpha' has no token in AH_CX_TOKEN_alpha"),
        (
            "keyed",
            OAUTH_ENV,
            "AH_CX_TOKEN_keyed holds an api key; put it in CODEX_API_KEY and route api instead",
        ),
        ("bare", OAUTH_ENV, "AH_CX_TOKEN_bare holds a ChatGPT token with no chatgpt_account_id claim"),
        (
            "alpha",
            {"AH_CX_TOKEN_alpha": _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": 7}})},
            "AH_CX_TOKEN_alpha holds a ChatGPT token with no chatgpt_account_id claim",
        ),
        (
            "alpha",
            {"AH_CX_TOKEN_alpha": _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": ""}})},
            "AH_CX_TOKEN_alpha holds a ChatGPT token with no chatgpt_account_id claim",
        ),
    ],
)
def test_a_missing_or_unsupported_credential_is_refused_by_name_only(name, environ, error):
    with pytest.raises(router.RoutingError) as raised:
        router.credential(_token(name), environ)
    assert str(raised.value) == error


def test_only_an_ordinary_chatgpt_token_takes_the_bearer_provider():
    assert router.credential(_token("alpha"), OAUTH_ENV) == router.CodexAccount(
        "alpha", "AH_CX_TOKEN_alpha", bearer=True
    )
    assert router.credential(_token("gamma"), OAUTH_ENV) == _token("gamma")
    assert router.credential(_token("delta"), OAUTH_ENV) == _token("delta")


def test_the_bearer_overrides_name_the_chatgpt_backend_and_only_variable_names():
    provider = "model_providers.agentihooks-chatgpt"
    assert router.bearer_overrides() == [
        "-c",
        'model_provider="agentihooks-chatgpt"',
        "-c",
        f'{provider}.name="ChatGPT"',
        "-c",
        f'{provider}.base_url="https://chatgpt.com/backend-api/codex"',
        "-c",
        f'{provider}.env_key="CODEX_ACCESS_TOKEN"',
        "-c",
        f'{provider}.wire_api="responses"',
        "-c",
        f"{provider}.requires_openai_auth=false",
        "-c",
        f"{provider}.supports_websockets=false",
        "-c",
        f'{provider}.env_http_headers.chatgpt-account-id="AGENTIHOOKS_CHATGPT_ACCOUNT_ID"',
    ]


def test_a_bearer_child_carries_its_token_and_account_id_and_runs_the_bearer_provider():
    account = router.credential(_token("alpha"), OAUTH_ENV)
    child = router.child_environment(account, OAUTH_ENV)
    assert child["CODEX_ACCESS_TOKEN"] == OAUTH
    assert child["AH_CX_TOKEN_alpha"] == OAUTH
    assert child["AGENTIHOOKS_CHATGPT_ACCOUNT_ID"] == "acct-1"
    assert not [name for name in child if name.startswith("AH_CX_TOKEN_") and name != "AH_CX_TOKEN_alpha"]
    assert router.command(account, "/usr/bin/codex", ["exec", "hi"]) == [
        "/usr/bin/codex",
        "--no-daemon",
        *router.bearer_overrides(),
        "exec",
        "hi",
    ]
    second = router.child_environment(router.credential(_token("beta"), OAUTH_ENV), OAUTH_ENV)
    assert (second["CODEX_ACCESS_TOKEN"], second["AGENTIHOOKS_CHATGPT_ACCOUNT_ID"]) == (OAUTH_TWO, "acct-2")


def test_default_api_and_access_token_children_never_inherit_an_account_id():
    for account in (router.CodexAccount("default"), _token("gamma"), _token("delta")):
        child = router.child_environment(router.credential(account, OAUTH_ENV), OAUTH_ENV)
        assert "AGENTIHOOKS_CHATGPT_ACCOUNT_ID" not in child
    api_env = {**OAUTH_ENV, "CODEX_API_KEY": "key"}
    assert "AGENTIHOOKS_CHATGPT_ACCOUNT_ID" not in router.child_environment(router.api_account(api_env), api_env)
    gamma = router.child_environment(_token("gamma"), OAUTH_ENV)
    assert gamma["CODEX_ACCESS_TOKEN"] == AGENT
    assert router.command(_token("gamma"), "/usr/bin/codex", ["-m", "o3"]) == [
        "/usr/bin/codex",
        "--no-daemon",
        "-m",
        "o3",
    ]


def test_the_slot_child_of_a_chatgpt_account_carries_its_account_id():
    source = router.CodexAccountSource()
    child = source.child_env(router.Slot("codex", "alpha", 6, 0, kind="subscription"), OAUTH_ENV)
    assert (child["CODEX_ACCESS_TOKEN"], child["AGENTIHOOKS_CHATGPT_ACCOUNT_ID"]) == (OAUTH, "acct-1")
    with pytest.raises(router.RoutingError) as raised:
        source.child_env(router.Slot("codex", "keyed", 6, 0, kind="subscription"), OAUTH_ENV)
    assert str(raised.value) == "AH_CX_TOKEN_keyed holds an api key; put it in CODEX_API_KEY and route api instead"


def test_the_probe_of_a_chatgpt_account_runs_the_bearer_provider_and_skips_an_unsupported_one(monkeypatch):
    seen = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs["env"]))
        return subprocess.CompletedProcess(argv, 0, '{"type": "thread.started", "thread_id": "t-9"}\n', "")

    monkeypatch.setattr(router.shutil, "which", lambda name: "/usr/bin/codex")
    monkeypatch.setattr(router.codex_quota, "session_quota", lambda environ, thread: thread)
    assert router.probe(_token("alpha"), OAUTH_ENV, run) == "t-9"
    argv, env = seen[0]
    assert argv == ["/usr/bin/codex", "--no-daemon", *router.bearer_overrides(), *router.PROBE_ARGS]
    assert env["AGENTIHOOKS_CHATGPT_ACCOUNT_ID"] == "acct-1"
    assert router.probe(_token("keyed"), OAUTH_ENV, run) is None
    assert router.probe(_token("ghost"), OAUTH_ENV, run) is None
    assert len(seen) == 1


def test_a_forced_chatgpt_launch_runs_the_bearer_provider_and_never_prints_the_token(monkeypatch, tmp_path, capsys):
    rc, seen, report = _launch(monkeypatch, tmp_path, OAUTH_ENV, ["--route", "beta", "exec", "hi"])
    assert rc == 0
    assert seen["cmd"] == ["/usr/bin/codex", "--no-daemon", *router.bearer_overrides(), "exec", "hi"]
    assert (seen["env"]["CODEX_ACCESS_TOKEN"], seen["env"]["AGENTIHOOKS_CHATGPT_ACCOUNT_ID"]) == (OAUTH_TWO, "acct-2")
    assert report.read_text() == "status=routed\naccount=beta\nplacement=forced\n"
    out = capsys.readouterr()
    assert OAUTH_TWO not in out.out + out.err and "acct-2" not in out.out + out.err


def test_an_agent_identity_launch_is_unchanged(monkeypatch, tmp_path):
    rc, seen, report = _launch(monkeypatch, tmp_path, OAUTH_ENV, ["--route", "gamma", "-m", "o3"])
    assert rc == 0
    assert seen["cmd"] == ["/usr/bin/codex", "--no-daemon", "-m", "o3"]
    assert seen["env"]["CODEX_ACCESS_TOKEN"] == AGENT
    assert "AGENTIHOOKS_CHATGPT_ACCOUNT_ID" not in seen["env"]


def test_an_open_route_to_a_chatgpt_account_runs_the_bearer_provider(monkeypatch, tmp_path):
    environ = {"HOME": "/home/u", "AH_CX_TOKEN_alpha": OAUTH, "AH_CX_TOKEN_gamma": AGENT}
    quotas = {"default": _quota(90.0), "alpha": _quota(10.0), "gamma": _quota(50.0)}
    rc, seen, report = _launch(monkeypatch, tmp_path, environ, ["-m", "o3"], quotas)
    assert rc == 0 and report.read_text() == "status=routed\naccount=alpha\nplacement=open\n"
    assert seen["cmd"] == ["/usr/bin/codex", "--no-daemon", *router.bearer_overrides(), "-m", "o3"]
    assert seen["env"]["AGENTIHOOKS_CHATGPT_ACCOUNT_ID"] == "acct-1"


def test_an_unsupported_credential_refuses_the_launch_and_reports_it(monkeypatch, tmp_path, capsys):
    rc, seen, report = _launch(monkeypatch, tmp_path, OAUTH_ENV, ["--route", "keyed", "-m", "o3"])
    error = "AH_CX_TOKEN_keyed holds an api key; put it in CODEX_API_KEY and route api instead"
    assert (rc, seen) == (3, {})
    assert report.read_text() == f"status=failed\nerror={error}\n"
    out = capsys.readouterr()
    assert out.err == f"agentihooks codex: {error}\n"
    assert "sk-proj-value" not in out.out + out.err
    rc, seen, report = _launch(monkeypatch, tmp_path, OAUTH_ENV, ["--route", "bare"])
    assert (rc, seen) == (3, {})
    assert report.read_text() == (
        "status=failed\nerror=AH_CX_TOKEN_bare holds a ChatGPT token with no chatgpt_account_id claim\n"
    )
