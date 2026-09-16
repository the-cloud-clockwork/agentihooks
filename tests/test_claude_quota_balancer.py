import json
import subprocess
import sys

from scripts import claude_quota_balancer as balancer


def _stream(account_usage: float, weekly_usage: float) -> str:
    events = [
        {"type": "system", "subtype": "init", "model": "claude-haiku-4-5-20251001"},
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": "allowed_warning" if weekly_usage >= 0.75 else "allowed",
                "unifiedWindows": {
                    "five_hour": {"utilization": account_usage, "resetsAt": 4600},
                    "seven_day": {"utilization": weekly_usage, "resetsAt": 91000},
                },
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


def test_table_orders_margin_and_shows_resets():
    high = balancer.parse_probe("HIGH", _stream(0.20, 0.30), 100)
    drain = balancer.parse_probe("DRAIN", _stream(0.05, 0.96), 200)

    table = balancer.render_table([drain, high], now=1000)

    assert table.index("HIGH") < table.index("DRAIN")
    assert "70%" in table
    assert "1h00m" in table
    assert "1000/200000 (0%)" in table


def test_dry_run_prints_total_execution_time(monkeypatch, capsys):
    result = balancer.parse_probe("ALPHA", _stream(0.20, 0.30), 100)
    elapsed = iter([10.0, 12.345])
    monkeypatch.setattr(sys, "argv", ["claude_quota_balancer.py", "--dry-run"])
    monkeypatch.setattr(
        balancer, "discover_credentials", lambda environ: [balancer.Credential("AH_CC_TOKEN_ALPHA", "secret")]
    )
    monkeypatch.setattr(balancer, "probe_credential", lambda *args: result)
    monkeypatch.setattr(balancer.time, "monotonic", lambda: next(elapsed))

    assert balancer.main() == 0
    assert "Dry-run execution time: 2.35s" in capsys.readouterr().out
