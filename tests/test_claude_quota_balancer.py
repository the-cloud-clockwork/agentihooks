import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from scripts import claude_quota_balancer as balancer
from scripts.routing import place
from scripts.routing.slots import API, API_UNBOUNDED, SUBSCRIPTION


def _stream(account_usage: float, weekly_usage: float, fable_usage: float | None = None) -> str:
    windows = {
        "five_hour": {"utilization": account_usage, "resetsAt": 4600},
        "seven_day": {"utilization": weekly_usage, "resetsAt": 91000},
    }
    if fable_usage is not None:
        windows["seven_day_overage_included"] = {"utilization": fable_usage, "resetsAt": 91000}
    events = [
        {"type": "system", "subtype": "init", "model": "claude-haiku-4-5-20251001"},
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "allowed_warning" if max(weekly_usage, fable_usage or 0) >= 0.75 else "allowed",
                "unifiedWindows": windows,
            },
        },
        {
            "type": "result",
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 990,
                "cache_read_input_tokens": 0,
            },
            "modelUsage": {"claude-haiku-4-5-20251001": {"contextWindow": 200000}},
        },
    ]
    return "\n".join(json.dumps(event) for event in events)


def test_discovers_dynamic_token_names_without_exposing_values():
    credentials = balancer.discover_credentials(
        {
            "AH_CC_TOKEN_ZED": "secret-z",
            "AH_CC_TOKEN_ALPHA": "secret-a",
            "AH_CC_TOKEN_EMPTY": "",
            "CC_TOKEN_0": "old-secret",
        }
    )

    assert [credential.account for credential in credentials] == ["ALPHA", "ZED"]
    assert "secret" not in repr(credentials)


def test_account_metadata_slug_selects_exact_environment_suffix():
    credentials = [
        balancer.Credential("AH_CC_TOKEN_ALPHA", "secret-a"),
        balancer.Credential("AH_CC_TOKEN_ALPHA_2", "secret-b"),
    ]

    assert balancer.credential_for_slug(credentials, "ALPHA").env_name == "AH_CC_TOKEN_ALPHA"
    try:
        balancer.credential_for_slug(credentials, "MISSING")
    except balancer.RoutingError as exc:
        assert "available: ALPHA, ALPHA_2" in str(exc)
    else:
        raise AssertionError("missing suffix should fail")


def test_ranks_by_the_tightest_quota_window():
    high = balancer.parse_probe("HIGH", _stream(0.20, 0.30), 100)
    reduce = balancer.parse_probe("REDUCE", _stream(0.10, 0.70), 100)
    drain = balancer.parse_probe("DRAIN", _stream(0.05, 0.96), 100)

    ranked = balancer.rank_results([drain, reduce, high])

    assert [(result.account, result.state, result.margin) for result in ranked] == [
        ("HIGH", "NORMAL", 70.0),
        ("REDUCE", "REDUCE", 30.0),
        ("DRAIN", "DRAIN", 4.0),
    ]
    assert high.context_used == 1000
    assert high.context_capacity == 200000


def test_fable_mode_includes_separate_quota_in_margin_and_table():
    result = balancer.parse_probe("FABLE", _stream(0.20, 0.30, 0.85), 100, include_fable=True)

    assert result.fable.remaining == 15.0
    assert result.margin == 15.0
    assert result.state == "DRAIN_SOON"
    table = balancer.render_table([result], now=1000, include_fable=True)
    assert "FABLE LEFT" in table
    assert "FABLE RESET" in table
    assert "15%" in table


def test_probe_passes_only_selected_oauth_token(monkeypatch):
    observed = {}

    def run(command, **kwargs):
        observed["command"] = command
        observed["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, _stream(0.20, 0.30), "")

    monkeypatch.setattr(balancer.subprocess, "run", run)
    credential = balancer.Credential("AH_CC_TOKEN_ALPHA", "selected-secret")
    result = balancer.probe_credential(
        credential,
        "haiku",
        10,
        {
            "AH_CC_TOKEN_ALPHA": "selected-secret",
            "AH_CC_TOKEN_BETA": "peer-secret",
            "ANTHROPIC_API_KEY": "api-secret",
            "PATH": "/bin",
        },
    )

    assert result.state == "NORMAL"
    assert observed["env"] == {"PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": "selected-secret"}
    assert "selected-secret" not in observed["command"]


def test_probes_run_concurrently_with_three_workers(monkeypatch):
    barrier = threading.Barrier(3)

    def probe(credential, *args, **kwargs):
        barrier.wait(timeout=5)
        return balancer.parse_probe(credential.account, _stream(0.20, 0.30), 100)

    monkeypatch.setattr(balancer, "probe_credential", probe)
    credentials = [balancer.Credential(f"AH_CC_TOKEN_{name}", "secret") for name in ("A", "B", "C")]

    results = balancer.probe_credentials(credentials, "haiku", 10, {})

    assert [result.account for result in results] == ["A", "B", "C"]


def test_table_orders_margin_and_shows_resets():
    high = balancer.parse_probe("HIGH", _stream(0.20, 0.30), 100)
    drain = balancer.parse_probe("DRAIN", _stream(0.05, 0.96), 200)

    table = balancer.render_table([drain, high], now=1000)

    assert table.index("HIGH") < table.index("DRAIN")
    assert "70%" in table
    assert "1h00m" in table
    assert "5H LEFT" in table
    assert "7D LEFT" in table
    assert "ROUTING LEFT" in table
    assert "20%/80%" not in table
    assert "5%/95%" not in table
    assert "MODEL" not in table
    assert "LATENCY" not in table
    assert "PROBE CTX" not in table
    assert "FABLE LEFT" not in table


def test_dry_run_prints_total_execution_time(monkeypatch, capsys):
    result = balancer.parse_probe("ALPHA", _stream(0.20, 0.30, 0.40), 100, include_fable=True)
    elapsed = iter([10.0, 12.345])
    monkeypatch.setattr(sys, "argv", ["claude_quota_balancer.py", "--dry-run", "--fable"])
    monkeypatch.setattr(
        balancer, "discover_credentials", lambda environ: [balancer.Credential("AH_CC_TOKEN_ALPHA", "secret")]
    )

    monkeypatch.setattr(balancer, "collect_results", lambda *args, **kwargs: ([result], "live"))
    monkeypatch.setattr(balancer.time, "monotonic", lambda: next(elapsed))

    assert balancer.main() == 0
    output = capsys.readouterr().out
    assert "Dry-run execution time: 2.35s (accounts=1, max_workers=1, source=live)" in output
    assert "FABLE LEFT" in output


def test_cache_reuses_fresh_results_and_probes_new_accounts(monkeypatch, tmp_path):
    calls = []

    def probe(credentials, *args, **kwargs):
        calls.append([credential.account for credential in credentials])
        return [balancer.parse_probe(credential.account, _stream(0.20, 0.30), 100) for credential in credentials]

    monkeypatch.setattr(balancer, "probe_credentials", probe)
    cache = tmp_path / "cache.json"
    alpha = balancer.Credential("AH_CC_TOKEN_ALPHA", "secret-a")
    beta = balancer.Credential("AH_CC_TOKEN_BETA", "secret-b")

    first, first_source = balancer.collect_results([alpha], cache_file=cache, environ={}, now=1000)
    second, second_source = balancer.collect_results([alpha], cache_file=cache, environ={}, now=1030)
    third, third_source = balancer.collect_results([alpha, beta], cache_file=cache, environ={}, now=1040)

    assert [result.account for result in first] == ["ALPHA"]
    assert [result.account for result in second] == ["ALPHA"]
    assert [result.account for result in third] == ["ALPHA", "BETA"]
    assert (first_source, second_source, third_source) == ("live", "cached", "live")
    assert calls == [["ALPHA"], ["BETA"]]
    assert "secret" not in cache.read_text()


def test_cache_refreshes_after_quota_reset(monkeypatch, tmp_path):
    calls = []

    def probe(credentials, *args, **kwargs):
        calls.append([credential.account for credential in credentials])
        return [balancer.parse_probe(credential.account, _stream(0.20, 0.30), 100) for credential in credentials]

    monkeypatch.setattr(balancer, "probe_credentials", probe)
    cache = tmp_path / "cache.json"
    credential = balancer.Credential("AH_CC_TOKEN_ALPHA", "secret")

    balancer.collect_results([credential], cache_file=cache, environ={}, now=4550)
    _, source = balancer.collect_results([credential], cache_file=cache, environ={}, now=4601)

    assert source == "live"
    assert calls == [["ALPHA"], ["ALPHA"]]


def test_corrupt_cache_is_replaced(monkeypatch, tmp_path):
    cache = tmp_path / "cache.json"
    cache.write_text("{broken")
    credential = balancer.Credential("AH_CC_TOKEN_ALPHA", "secret")
    monkeypatch.setattr(
        balancer,
        "probe_credentials",
        lambda credentials, *args, **kwargs: [
            balancer.parse_probe(item.account, _stream(0.20, 0.30), 100) for item in credentials
        ],
    )

    results, source = balancer.collect_results([credential], cache_file=cache, environ={}, now=1000)

    assert source == "live"
    assert results[0].account == "ALPHA"
    assert json.loads(cache.read_text())["version"] == 1


def test_selection_fails_closed_when_every_week_is_under_five_percent(monkeypatch, tmp_path):
    result = balancer.parse_probe("ALPHA", _stream(0.10, 0.96), 100)
    monkeypatch.setattr(balancer, "collect_results", lambda *args, **kwargs: ([result], "live"))

    with pytest.raises(balancer.RoutingError, match="free session under its quota band") as raised:
        balancer.select_credential({"AH_CC_TOKEN_ALPHA": "secret"}, cache_file=tmp_path / "cache.json", now=1000)
    assert raised.value.results == [result]


def test_the_week_floor_is_five_percent_left(monkeypatch, tmp_path):
    floor = balancer.parse_probe("FLOOR", _stream(0.10, 0.95), 100)
    under = balancer.parse_probe("UNDER", _stream(0.10, 0.951), 100)
    monkeypatch.setattr(balancer, "collect_results", lambda *args, **kwargs: ([under, floor], "live"))

    decision = balancer.select_credential(
        {"AH_CC_TOKEN_FLOOR": "floor-secret", "AH_CC_TOKEN_UNDER": "under-secret"},
        cache_file=tmp_path / "cache.json",
        now=1000,
    )

    assert decision.credential.env_name == "AH_CC_TOKEN_FLOOR"
    assert (decision.sessions, decision.max_sessions) == (0, 6)
    assert not balancer.is_routable(under, now=1000)
    assert balancer.is_routable(floor, now=1000)


def test_selection_reports_the_account_against_its_band_cap(monkeypatch, tmp_path):
    low = balancer.parse_probe("LOW", _stream(0.40, 0.60), 100)
    high = balancer.parse_probe("HIGH", _stream(0.20, 0.30), 100)
    monkeypatch.setattr(balancer, "collect_results", lambda *args, **kwargs: ([low, high], "cached"))

    decision = balancer.select_credential(
        {"AH_CC_TOKEN_LOW": "low-secret", "AH_CC_TOKEN_HIGH": "high-secret"},
        cache_file=tmp_path / "cache.json",
        sessions={"HIGH": 1},
        now=1000,
    )

    assert decision.credential.env_name == "AH_CC_TOKEN_LOW"
    assert decision.source == "cached"
    assert "low-secret" not in repr(decision)
    assert balancer.format_selection(decision) == (
        "[agenti] account=LOW routing_left=40% 5h_left=60% 7d_left=40% sessions=0/6 source=cached"
    )


def test_fable_detection_prefers_explicit_model_then_settings(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"model": "claude-fable-5"}))

    assert balancer.route_requires_fable([], settings)
    assert balancer.route_requires_fable(["--model", "fable"], Path("/missing"))
    assert balancer.route_requires_fable(["--model=claude-fable-5"], Path("/missing"))
    assert not balancer.route_requires_fable(["--model", "sonnet"], settings)


def test_fable_selection_requires_fable_window(monkeypatch, tmp_path):
    incomplete = balancer.parse_probe("ALPHA", _stream(0.20, 0.30), 100, include_fable=True)
    monkeypatch.setattr(balancer, "collect_results", lambda *args, **kwargs: ([incomplete], "live"))

    try:
        balancer.select_credential(
            {"AH_CC_TOKEN_ALPHA": "secret"},
            include_fable=True,
            cache_file=tmp_path / "cache.json",
        )
    except balancer.RoutingError:
        pass
    else:
        raise AssertionError("Fable routing should require the Fable quota window")


def test_account_metadata_captures_all_json_and_redacts_tokens(monkeypatch):
    credential = balancer.Credential("AH_CC_TOKEN_ALPHA", "oauth-secret")
    stdout = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init", "apiKeySource": "oauth", "value": "oauth-secret"}),
            json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}),
            "not-json oauth-secret",
        ]
    )

    def run(command, **kwargs):
        assert "--include-partial-messages" in command
        return subprocess.CompletedProcess(command, 0, stdout, "stderr oauth-secret")

    monkeypatch.setattr(balancer.subprocess, "run", run)

    metadata = balancer.collect_account_metadata(
        [credential],
        environ={"AH_CC_TOKEN_ALPHA": "oauth-secret"},
    )

    encoded = json.dumps(metadata)
    assert "oauth-secret" not in encoded
    account = metadata["accounts"][0]
    assert account["events"][0]["subtype"] == "init"
    assert account["events"][1]["type"] == "rate_limit_event"
    assert account["non_json_stdout"] == ["not-json <redacted>"]
    assert account["stderr"] == "stderr <redacted>"


def test_show_account_metadata_prints_json_instead_of_table(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["claude_quota_balancer.py", "--show-account-metadata=ALPHA"])
    monkeypatch.setattr(
        balancer,
        "discover_credentials",
        lambda environ: [balancer.Credential("AH_CC_TOKEN_ALPHA", "secret")],
    )
    payload = {
        "schema_version": 1,
        "generated_at": "now",
        "probe_model": "haiku",
        "account_count": 1,
        "accounts": [{"account": "ALPHA", "return_code": 0, "events": [{"type": "system"}]}],
    }
    observed = {}

    def collect(credentials, **kwargs):
        observed["accounts"] = [credential.account for credential in credentials]
        return payload

    monkeypatch.setattr(balancer, "collect_account_metadata", collect)

    assert balancer.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output == payload
    assert observed == {"accounts": ["ALPHA"]}


def test_session_account_matches_oauth_token_before_sole_token():
    credentials = [
        balancer.Credential("AH_CC_TOKEN_ALPHA", "secret-a"),
        balancer.Credential("AH_CC_TOKEN_BETA", "secret-b"),
    ]

    by_env = balancer.identify_session_account({"CLAUDE_CODE_OAUTH_TOKEN": "secret-b"}, credentials)
    by_ancestor = balancer.identify_session_account({"AH_CC_TOKEN_ALPHA": "secret-a"}, credentials, "secret-b")
    sole = balancer.identify_session_account({"AH_CC_TOKEN_ALPHA": "secret-a"}, credentials)
    unmatched = balancer.identify_session_account({}, credentials, "other")
    unrouted = balancer.identify_session_account({"AH_CC_TOKEN_ALPHA": "a", "AH_CC_TOKEN_BETA": "b"}, credentials)

    assert by_env == balancer.SessionAccount("BETA", "oauth-token")
    assert by_ancestor == balancer.SessionAccount("BETA", "oauth-token")
    assert sole == balancer.SessionAccount("ALPHA", "sole-token")
    assert unmatched == balancer.SessionAccount("", "oauth-token-unmatched")
    assert unrouted == balancer.SessionAccount("", "unrouted")


@pytest.mark.skipif(not Path("/proc/self/stat").is_file(), reason="requires /proc")
def test_ancestor_oauth_token_reads_nearest_parent_process():
    grandchild = (
        "from scripts.claude_quota_balancer import ancestor_oauth_token; print(ancestor_oauth_token() == 'canary')"
    )
    child = f"import subprocess, sys; subprocess.run([sys.executable, '-c', {grandchild!r}], env={{'PATH': ''}}, cwd={str(Path.cwd())!r})"
    env = {"CLAUDE_CODE_OAUTH_TOKEN": "canary", "PYTHONPATH": str(Path(__file__).parents[1])}

    completed = subprocess.run([sys.executable, "-c", child], env=env, capture_output=True, text=True, check=True)

    assert completed.stdout.strip() == "True"


def test_probing_one_account_keeps_other_cached_accounts(monkeypatch, tmp_path):
    monkeypatch.setattr(
        balancer,
        "probe_credentials",
        lambda credentials, *args, **kwargs: [
            balancer.parse_probe(item.account, _stream(0.20, 0.30), 100) for item in credentials
        ],
    )
    cache = tmp_path / "cache.json"
    alpha = balancer.Credential("AH_CC_TOKEN_ALPHA", "secret-a")
    beta = balancer.Credential("AH_CC_TOKEN_BETA", "secret-b")

    balancer.collect_results([alpha, beta], cache_file=cache, environ={}, now=1000)
    balancer.collect_results([alpha], cache_file=cache, environ={}, refresh=True, now=1100)
    observations = balancer.cached_observations(cache_file=cache)

    assert sorted((result.account, seen) for seen, result in observations) == [("ALPHA", 1100.0), ("BETA", 1000.0)]


def test_table_marks_current_account_and_cache_age():
    alpha = balancer.parse_probe("ALPHA", _stream(0.20, 0.30), 100)
    beta = balancer.parse_probe("BETA", _stream(0.10, 0.40), 100)

    table = balancer.render_table([alpha, beta], now=1000, current="BETA", observed={"ALPHA": 400, "BETA": 1000})

    assert "BETA (current)" in table
    assert "ALPHA (current)" not in table
    assert "AGE" in table
    assert "10m" in table


def test_get_current_balance_skill_runs_the_current_flag():
    skill = Path(__file__).parents[1] / "profiles" / "package" / "skills" / "get-agents-quota" / "SKILL.md"
    text = skill.read_text()

    assert "name: get-agents-quota" in text
    assert "agentihooks balance --current" in text


def _three(monkeypatch):
    best = balancer.parse_probe("BEST", _stream(0.10, 0.20), 100)
    mid = balancer.parse_probe("MID", _stream(0.30, 0.40), 100)
    low = balancer.parse_probe("LOW", _stream(0.50, 0.70), 100)
    monkeypatch.setattr(balancer, "collect_results", lambda *args, **kwargs: ([low, mid, best], "cached"))
    return {"AH_CC_TOKEN_BEST": "b", "AH_CC_TOKEN_MID": "m", "AH_CC_TOKEN_LOW": "l"}


def _pick(env, tmp_path, sessions=None, **kwargs):
    return balancer.select_credential(env, cache_file=tmp_path / "c.json", sessions=sessions, now=1000, **kwargs)


def test_the_account_with_the_fewest_live_sessions_wins(monkeypatch, tmp_path):
    env = _three(monkeypatch)

    decision = _pick(env, tmp_path, {"BEST": 2, "MID": 1})

    assert decision.result.account == "LOW"
    assert balancer.format_selection(decision) == (
        "[agenti] account=LOW routing_left=30% 5h_left=50% 7d_left=30% sessions=0/4 source=cached"
    )


def test_ties_go_in_account_order_and_a_full_band_yields(monkeypatch, tmp_path):
    env = _three(monkeypatch)

    assert _pick(env, tmp_path).result.account == "BEST"
    assert _pick(env, tmp_path, {"BEST": 2, "MID": 1, "LOW": 4}).result.account == "MID"


def test_equal_sessions_go_to_the_account_whose_week_resets_soonest(monkeypatch, tmp_path):
    five = balancer.QuotaWindow(used=10, resets_at=5000)
    results = [
        balancer.ProbeResult(name, "allowed", "NORMAL", 50, five, balancer.QuotaWindow(used=50, resets_at=reset))
        for name, reset in (("A", 90000), ("B", 3000), ("C", None), ("D", 999))
    ]
    monkeypatch.setattr(balancer, "collect_results", lambda *args, **kwargs: (results, "cached"))
    env = {f"AH_CC_TOKEN_{name}": name.lower() for name in "ABCD"}

    assert _pick(env, tmp_path).result.account == "B"
    assert _pick(env, tmp_path, {"B": 1}).result.account == "A"
    assert _pick(env, tmp_path, {"A": 1, "B": 1}).result.account == "C"
    assert _pick(env, tmp_path, {"A": 1, "B": 1, "C": 1}).result.account == "D"


def test_an_account_at_the_five_hour_margin_spends_last_until_its_window_resets(monkeypatch, tmp_path):
    week = balancer.QuotaWindow(used=50, resets_at=3000)
    other = balancer.ProbeResult(
        "B", "allowed", "NORMAL", 50, balancer.QuotaWindow(used=10), balancer.QuotaWindow(used=50, resets_at=90000)
    )
    tired = balancer.ProbeResult("T", "allowed", "NORMAL", 5, balancer.QuotaWindow(used=95, resets_at=5000), week)
    rested = balancer.ProbeResult("R", "allowed", "NORMAL", 50, balancer.QuotaWindow(used=97, resets_at=999), week)
    env = {f"AH_CC_TOKEN_{name}": name.lower() for name in "BTR"}

    monkeypatch.setattr(balancer, "collect_results", lambda *args, **kwargs: ([tired, other], "cached"))
    assert _pick(env, tmp_path).result.account == "B"
    monkeypatch.setattr(balancer, "collect_results", lambda *args, **kwargs: ([rested, other], "cached"))
    assert _pick(env, tmp_path).result.account == "R"


def test_every_account_at_its_band_cap_refuses_the_launch(monkeypatch, tmp_path):
    env = _three(monkeypatch)

    with pytest.raises(balancer.RoutingError, match="free session under its quota band"):
        _pick(env, tmp_path, {"BEST": 6, "MID": 6, "LOW": 4})


def test_excluded_account_is_never_selected(monkeypatch, tmp_path):
    env = _three(monkeypatch)

    assert _pick(env, tmp_path, exclude=["BEST"]).result.account == "LOW"
    with pytest.raises(balancer.RoutingError, match="outside BEST, LOW, MID"):
        _pick(env, tmp_path, exclude=["BEST", "MID", "LOW"])


def test_table_shows_live_sessions_against_each_band_cap():
    alpha = balancer.parse_probe("alpha", _stream(0.10, 0.20), 100)
    spent = balancer.parse_probe("spent", _stream(0.10, 0.99), 100)
    broken = balancer._error_result("broken", "probe failed")

    table = balancer.render_table([alpha, spent, broken], now=0, sessions={"alpha": 1, "unrouted": 2})
    rows = {line.split()[1]: line for line in table.splitlines()[2:5]}

    assert "SESSIONS" in table.splitlines()[0]
    assert " 1/6 " in rows["alpha"]
    assert " 0/0 " in rows["spent"]
    assert " 0/? " in rows["broken"]
    assert table.splitlines()[-1] == "unrouted: 2 session(s)"


def test_reserve_account_is_chosen_only_when_no_other_has_room(monkeypatch, tmp_path):
    env = _three(monkeypatch)

    assert _pick({**env, "AGENTIHOOKS_RESERVE_ACCOUNTS": "BEST"}, tmp_path).result.account == "LOW"
    assert _pick({**env, "AGENTIHOOKS_RESERVE_ACCOUNTS": "BEST,MID,LOW"}, tmp_path).result.account == "BEST"
    assert _pick({**env, "AGENTIHOOKS_RESERVE_ACCOUNTS": "BEST, LOW"}, tmp_path).result.account == "MID"
    with pytest.raises(balancer.RoutingError, match=r"^no Claude account has a free session under its quota band$"):
        _pick(env, tmp_path, {"BEST": 6, "MID": 6, "LOW": 6})
    offered = []
    with monkeypatch.context() as patched:
        patched.setattr(balancer.session_bands, "pick", lambda seats: offered.extend(seats))
        with pytest.raises(balancer.RoutingError):
            _pick(env, tmp_path)
    assert {seat.harness for seat in offered} == {"claude"}
    reserved = {**env, "AGENTIHOOKS_RESERVE_ACCOUNTS": "LOW"}
    assert _pick(reserved, tmp_path, {"BEST": 6, "MID": 6}).result.account == "LOW"
    assert _pick(reserved, tmp_path, {"BEST": 6, "MID": 2}).result.account == "MID"


NOW = 1_800_000_000


def _today(account: str, five_left: float, week_left: float, week_hours: float) -> balancer.ProbeResult:
    five = balancer.QuotaWindow(used=100 - five_left, resets_at=NOW + 2 * 3600)
    week = balancer.QuotaWindow(used=100 - week_left, resets_at=NOW + int(week_hours * 3600))
    return balancer.ProbeResult(account, "allowed", "NORMAL", min(five_left, week_left), five, week)


TODAY = [
    _today("ncgma", 75, 93, 159.9),
    _today("nchotma", 95, 8, 34.9),
    _today("ncsmgma", 100, 4, 4.9),
    _today("nctcc", 100, 12, 71.9),
    _today("tccgma", 80, 19, 89.9),
]


def test_todays_accounts_get_band_caps_and_the_next_session_rotates(monkeypatch, tmp_path):
    assert {result.account: balancer.account_cap(result, NOW) for result in TODAY} == {
        "ncgma": 6,
        "nchotma": 6,
        "ncsmgma": 0,
        "nctcc": 6,
        "tccgma": 6,
    }
    monkeypatch.setattr(balancer, "collect_results", lambda *args, **kwargs: (TODAY, "live"))
    env = {f"AH_CC_TOKEN_{result.account}": "secret" for result in TODAY}
    live = {"ncgma": 4, "nchotma": 2, "tccgma": 3}
    order = []
    for _ in range(4):
        decision = balancer.select_credential(env, cache_file=tmp_path / "c.json", sessions=live, now=NOW)
        order.append(decision.result.account)
        live = {**live, decision.result.account: live.get(decision.result.account, 0) + 1}
    assert order == ["nctcc", "nctcc", "nchotma", "nctcc"]


def test_a_reset_window_counts_as_full_and_a_rejected_account_takes_no_session():
    spent = _today("spent", 2, 50, 100)
    assert balancer.account_cap(spent, NOW) == 0
    assert balancer.account_cap(spent, NOW + 3 * 3600) == 6
    rejected = balancer.ProbeResult("x", "rejected", "BLOCKED", 0.0, TODAY[0].five_hour, TODAY[0].seven_day)
    assert balancer.account_cap(rejected, NOW) == 0
    assert balancer.account_cap(balancer._error_result("x", "failed"), NOW) is None


def test_fable_routing_caps_on_the_tighter_weekly_window():
    result = balancer.parse_probe("FABLE", _stream(0.20, 0.30, 0.97), 100, include_fable=True)
    assert balancer.account_cap(result, 1000) == 6
    assert balancer.account_cap(result, 1000, include_fable=True) == 0
    assert balancer.is_routable(result, 1000)
    assert not balancer.is_routable(result, 1000, include_fable=True)
    table = balancer.render_table([result], now=1000, include_fable=True, sessions={})
    assert " 0/0 " in table.splitlines()[2]


def test_a_week_that_reset_before_now_counts_as_full():
    spent = balancer.ProbeResult(
        "a", "allowed", "NORMAL", 1.0, balancer.QuotaWindow(10.0), balancer.QuotaWindow(99.0, 500)
    )
    assert balancer.account_cap(spent, 1000) == 6
    assert balancer.account_cap(spent, 400) == 0


def _weighted(monkeypatch, weight, cap=API_UNBOUNDED):
    def policy(harness, environ):
        assert harness == "claude"
        return place.ApiPolicy(weight, cap)

    monkeypatch.setattr(place, "policy", policy)


def test_the_api_share_counts_live_sessions_on_reserved_accounts(monkeypatch, tmp_path):
    env = {**_three(monkeypatch), "ANTHROPIC_API_KEY": "key", "AGENTIHOOKS_RESERVE_ACCOUNTS": "BEST"}
    _weighted(monkeypatch, 25)
    assert _pick(env, tmp_path, {"BEST": 4, "api": 1}).kind == API
    assert _pick(env, tmp_path, {"BEST": 2, "api": 1}).account == "LOW"


def test_an_unreadable_routing_setting_closes_only_the_api_side(monkeypatch, tmp_path, capsys):
    env = {**_three(monkeypatch), "ANTHROPIC_API_KEY": "key"}

    def unreadable(harness, environ):
        raise place.SettingsError("routing settings are unreadable: KeyError")

    monkeypatch.setattr(place, "policy", unreadable)
    assert _pick(env, tmp_path, {"BEST": 1, "MID": 1}).account == "LOW"
    assert capsys.readouterr().err == "[claude] api side closed: routing settings are unreadable: KeyError\n"
    with pytest.raises(balancer.RoutingError) as raised:
        _pick(env, tmp_path, {"BEST": 9, "MID": 9, "LOW": 9})
    assert str(raised.value) == "no Claude account has a free session under its quota band"


def test_a_token_named_api_is_reserved_for_the_api_route():
    env = {"AH_CC_TOKEN_api": "x", "AH_CC_TOKEN_": "y", "AH_CC_TOKEN_A": "a", "AH_CC_TOKEN_apix": "b"}
    assert [credential.account for credential in balancer.discover_credentials(env)] == ["A", "apix"]


def test_three_tokens_and_an_api_at_weight_25_give_api_one_of_every_four_sessions(monkeypatch, tmp_path):
    env = {**_three(monkeypatch), "ANTHROPIC_API_KEY": "key"}
    _weighted(monkeypatch, 25)
    sessions, kinds = {}, []
    for _ in range(8):
        decision = _pick(env, tmp_path, dict(sessions))
        kinds.append(decision.kind)
        sessions[decision.account] = sessions.get(decision.account, 0) + 1
    assert kinds == [API, SUBSCRIPTION, SUBSCRIPTION, SUBSCRIPTION] * 2
    assert sessions == {"api": 2, "BEST": 2, "MID": 2, "LOW": 2}


def test_with_every_token_week_below_five_percent_every_session_goes_to_api(monkeypatch, tmp_path):
    spent = [balancer.parse_probe(name, _stream(0.10, 0.97), 100) for name in ("A", "B", "C")]
    monkeypatch.setattr(balancer, "collect_results", lambda *args, **kwargs: (spent, "cached"))
    env = {"AH_CC_TOKEN_A": "a", "AH_CC_TOKEN_B": "b", "AH_CC_TOKEN_C": "c", "ANTHROPIC_API_KEY": "key"}
    _weighted(monkeypatch, 0)
    for live in range(4):
        decision = _pick(env, tmp_path, {"api": live})
        assert decision == balancer.RouteDecision(None, None, "cached", live, API_UNBOUNDED, kind=API)
        assert decision.account == "api"
    assert balancer.format_selection(decision) == "[agenti] account=api kind=api sessions=3/1000000 source=cached"


def test_an_api_endpoint_alone_routes_without_any_token(monkeypatch, tmp_path):
    monkeypatch.setattr(balancer, "collect_results", lambda *args, **kwargs: pytest.fail("probed without tokens"))
    _weighted(monkeypatch, 0)
    decision = _pick({"ANTHROPIC_API_KEY": "key"}, tmp_path)
    assert decision == balancer.RouteDecision(None, None, "cached", 0, API_UNBOUNDED, kind=API)


def test_without_an_api_endpoint_no_routing_setting_is_read(monkeypatch, tmp_path):
    env = _three(monkeypatch)
    monkeypatch.setattr(place, "policy", lambda harness, environ: pytest.fail("policy read without an api slot"))
    decision = _pick(env, tmp_path)
    assert (decision.kind, decision.account) == (SUBSCRIPTION, "BEST")
    with pytest.raises(balancer.RoutingError) as raised:
        _pick({}, tmp_path)
    assert str(raised.value) == "no non-empty AH_CC_TOKEN_* variables found"


def test_routing_fails_only_when_the_pool_and_the_api_are_both_closed(monkeypatch, tmp_path):
    env = {**_three(monkeypatch), "ANTHROPIC_API_KEY": "key"}
    full = {"BEST": 6, "MID": 6, "LOW": 4}
    _weighted(monkeypatch, 0, cap=2)
    assert _pick(env, tmp_path, {**full, "api": 1}).kind == API
    with pytest.raises(balancer.RoutingError) as raised:
        _pick(env, tmp_path, {**full, "api": 2})
    assert str(raised.value) == "no Claude account has a free session under its quota band"
    with pytest.raises(balancer.RoutingError) as raised:
        _pick(env, tmp_path, full, exclude=["api"])
    assert str(raised.value) == "no Claude account has a free session under its quota band outside api"


def test_a_reserved_account_is_used_before_the_api_at_weight_0(monkeypatch, tmp_path):
    env = {**_three(monkeypatch), "ANTHROPIC_API_KEY": "key", "AGENTIHOOKS_RESERVE_ACCOUNTS": "BEST,MID"}
    _weighted(monkeypatch, 0, cap=1)
    assert _pick(env, tmp_path, {"LOW": 4}).account == "BEST"
    assert _pick(env, tmp_path, {"LOW": 3}).account == "LOW"


def test_the_launch_time_reaches_the_api_side_and_an_excluded_api_has_no_weight(monkeypatch, tmp_path):
    env = {**_three(monkeypatch), "ANTHROPIC_API_KEY": "key"}
    seen = []

    def api_side(source, harness, environ, now):
        seen.append(now)
        return [], 0

    monkeypatch.setattr(place, "api_side", api_side)
    _pick(env, tmp_path)
    assert seen == [1000]
    assert balancer._api_side(env, {}, {"api"}, 1000) == ([], 0)


def test_an_api_pick_is_never_replaced_by_the_reserve_rule(monkeypatch, tmp_path):
    env = {**_three(monkeypatch), "ANTHROPIC_API_KEY": "key", "AGENTIHOOKS_RESERVE_ACCOUNTS": "api"}
    _weighted(monkeypatch, 100)
    assert _pick(env, tmp_path).kind == API


def test_route_api_resolves_to_the_api_endpoint_and_a_slug_to_its_token():
    env = {"AH_CC_TOKEN_A": "a", "ANTHROPIC_API_KEY": "key"}
    assert balancer.forced(env, "api") == balancer.RouteDecision(None, None, "forced", kind=API)
    assert balancer.forced(env, "A") == balancer.RouteDecision(
        balancer.Credential("AH_CC_TOKEN_A", "a"), None, "forced"
    )
    assert balancer.forced(env, "A").account == "A"
    with pytest.raises(balancer.RoutingError) as raised:
        balancer.forced({"AH_CC_TOKEN_A": "a"}, "api")
    assert str(raised.value) == "account 'api' needs an api endpoint; none is configured"


def test_an_api_launch_carries_no_subscription_token_and_the_route_marker():
    env = {"HOME": "/h", "AH_CC_TOKEN_A": "a", "CLAUDE_CODE_OAUTH_TOKEN": "oauth", "ANTHROPIC_API_KEY": "key"}
    decision = balancer.RouteDecision(None, None, "forced", kind=API)
    assert balancer.launch_environment(decision, env) == {
        "HOME": "/h",
        "ANTHROPIC_API_KEY": "key",
        "AH_ROUTE_API": "1",
        "AGENTIHOOKS_ROUTE_ACCOUNT": "api",
    }


def test_a_token_launch_carries_only_its_own_token_and_no_api_credential():
    env = {"HOME": "/h", "AH_CC_TOKEN_A": "a", "AH_CC_TOKEN_B": "b", "ANTHROPIC_API_KEY": "key", "AH_ROUTE_API": "1"}
    decision = balancer.RouteDecision(balancer.Credential("AH_CC_TOKEN_A", "a"), None, "forced")
    assert balancer.launch_environment(decision, env) == {
        "HOME": "/h",
        "AH_CC_TOKEN_A": "a",
        "CLAUDE_CODE_OAUTH_TOKEN": "a",
        "AGENTIHOOKS_ROUTE_ACCOUNT": "A",
    }
