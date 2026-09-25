import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from scripts import claude_quota_balancer as balancer


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


def test_selection_fails_closed_when_every_account_is_draining(monkeypatch, tmp_path):
    result = balancer.parse_probe("ALPHA", _stream(0.10, 0.96), 100)
    monkeypatch.setattr(balancer, "collect_results", lambda *args, **kwargs: ([result], "live"))

    try:
        balancer.select_credential(
            {"AH_CC_TOKEN_ALPHA": "secret"},
            cache_file=tmp_path / "cache.json",
        )
    except balancer.RoutingError as exc:
        assert exc.results == [result]
    else:
        raise AssertionError("selection should fail closed")


def test_selection_floor_is_five_percent_routing_left(monkeypatch, tmp_path):
    floor = balancer.parse_probe("FLOOR", _stream(0.10, 0.95), 100)
    under = balancer.parse_probe("UNDER", _stream(0.10, 0.951), 100)
    monkeypatch.setattr(balancer, "collect_results", lambda *args, **kwargs: ([under, floor], "live"))

    decision = balancer.select_credential(
        {"AH_CC_TOKEN_FLOOR": "floor-secret", "AH_CC_TOKEN_UNDER": "under-secret"},
        cache_file=tmp_path / "cache.json",
    )

    assert decision.credential.env_name == "AH_CC_TOKEN_FLOOR"
    assert decision.result.state == "DRAIN"
    assert not balancer.is_routable(under)


def test_selection_returns_highest_routing_left(monkeypatch, tmp_path):
    low = balancer.parse_probe("LOW", _stream(0.40, 0.60), 100)
    high = balancer.parse_probe("HIGH", _stream(0.20, 0.30), 100)
    monkeypatch.setattr(balancer, "collect_results", lambda *args, **kwargs: ([low, high], "cached"))

    decision = balancer.select_credential(
        {"AH_CC_TOKEN_LOW": "low-secret", "AH_CC_TOKEN_HIGH": "high-secret"},
        cache_file=tmp_path / "cache.json",
    )

    assert decision.credential.env_name == "AH_CC_TOKEN_HIGH"
    assert decision.result.routing_left == 70.0
    assert decision.source == "cached"
    assert "high-secret" not in repr(decision)
    assert balancer.format_selection(decision) == (
        "[agenti] account=HIGH routing_left=70% 5h_left=80% 7d_left=70% source=cached"
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
    skill = Path(__file__).parents[1] / "profiles" / "package" / "skills" / "get-current-balance" / "SKILL.md"
    text = skill.read_text()

    assert "name: get-current-balance" in text
    assert "agentihooks balance --current" in text


def _three(monkeypatch):
    best = balancer.parse_probe("BEST", _stream(0.10, 0.20), 100)
    mid = balancer.parse_probe("MID", _stream(0.30, 0.40), 100)
    low = balancer.parse_probe("LOW", _stream(0.50, 0.70), 100)
    monkeypatch.setattr(balancer, "collect_results", lambda *args, **kwargs: ([low, mid, best], "cached"))
    return {"AH_CC_TOKEN_BEST": "b", "AH_CC_TOKEN_MID": "m", "AH_CC_TOKEN_LOW": "l"}


def test_account_at_the_session_cap_yields_to_the_next_one(monkeypatch, tmp_path):
    env = _three(monkeypatch)

    decision = balancer.select_credential(
        env, cache_file=tmp_path / "c.json", sessions={"BEST": 2, "MID": 1}, max_sessions=2
    )

    assert decision.result.account == "MID"
    assert decision.placement == "open"
    assert balancer.format_selection(decision) == (
        "[agenti] account=MID routing_left=60% 5h_left=70% 7d_left=60% sessions=1/2 source=cached"
    )


def test_cap_is_configurable(monkeypatch, tmp_path):
    env = _three(monkeypatch)

    decision = balancer.select_credential(
        env, cache_file=tmp_path / "c.json", sessions={"BEST": 2, "MID": 1}, max_sessions=3
    )

    assert decision.result.account == "BEST"


def test_every_account_at_cap_overflows_to_the_least_loaded(monkeypatch, tmp_path):
    env = _three(monkeypatch)

    decision = balancer.select_credential(
        env, cache_file=tmp_path / "c.json", sessions={"BEST": 4, "MID": 2, "LOW": 2}, max_sessions=2
    )

    assert decision.result.account == "MID"
    assert decision.placement == "overflow"
    assert "placement=overflow" in balancer.format_selection(decision)


def test_excluded_account_is_never_selected(monkeypatch, tmp_path):
    env = _three(monkeypatch)

    decision = balancer.select_credential(env, cache_file=tmp_path / "c.json", exclude=["BEST"])
    assert decision.result.account == "MID"

    with pytest.raises(balancer.RoutingError, match="outside BEST, LOW, MID"):
        balancer.select_credential(env, cache_file=tmp_path / "c.json", exclude=["BEST", "MID", "LOW"])


def test_table_shows_live_sessions_per_account():
    alpha = balancer.parse_probe("alpha", _stream(0.10, 0.20), 100)

    table = balancer.render_table([alpha], now=0, sessions={"alpha": 1, "unrouted": 2}, max_sessions=2)

    assert "SESSIONS" in table.splitlines()[0]
    assert "1/2" in table.splitlines()[2]
    assert table.splitlines()[-1] == "unrouted: 2 session(s)"
