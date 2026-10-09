import argparse
import re

import pytest

from scripts import balance_cli
from scripts.routing import place
from scripts.routing.settings import FileSettings


def test_balance_set_writes_through_the_store_and_prints_before_and_after(monkeypatch, tmp_path, capsys):
    path = tmp_path / "routing-settings.json"
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@test")
    monkeypatch.setattr(place, "_client", lambda environ: None)

    assert balance_cli.cmd_balance_set(["claude-api-weight=30", "master-account-codex=2024"], now=5.0) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"store=file {path}",
        "claude-api-weight: 0 -> 30",
        "master-account-codex: unset -> 2024",
    ]
    store = FileSettings(path)
    assert store.get("claude-api-weight") == 30
    assert store.history()[-1] == {"key": "master-account-codex", "value": "2024", "actor": "engineer@test", "at": 5.0}
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME")
    monkeypatch.setattr(balance_cli.time, "time", lambda: 6.0)
    assert balance_cli.cmd_balance_set(["master-account-codex=none"]) == 0
    assert capsys.readouterr().out.splitlines() == [f"store=file {path}", "master-account-codex: 2024 -> unset"]
    assert store.history()[-1] == {"key": "master-account-codex", "value": None, "actor": "operator", "at": 6.0}

    assert balance_cli.cmd_balance_settings() == 0
    assert capsys.readouterr().out.splitlines() == [
        f"store=file {path}",
        "claude-api-weight=30",
        "codex-api-weight=0",
        "claude-api-max-sessions=unset",
        "codex-api-max-sessions=unset",
        "master-account-claude=unset",
        "master-account-codex=unset",
        "master-tier-claude=unset",
        "master-tier-codex=unset",
    ]


@pytest.mark.parametrize(
    ("pairs", "reason"),
    [
        (["claude-api-weight=101"], "invalid value for claude-api-weight: 101"),
        (["claude-api-weight=30.5"], "invalid value for claude-api-weight: 30.5"),
        (["codex-api-max-sessions=-1"], "invalid value for codex-api-max-sessions: -1"),
        (["nope=1"], "unknown routing setting nope"),
        (["claude-api-weight"], "expected KEY=VALUE, got claude-api-weight"),
        (["claude-api-weight=null"], "invalid value for claude-api-weight: null"),
        (["claude-api-weight=None"], "invalid value for claude-api-weight: None"),
        (["claude-api-weight="], "invalid value for claude-api-weight: "),
        (["master-account-claude="], "invalid value for master-account-claude: "),
    ],
)
def test_balance_set_refuses_an_invalid_pair_and_writes_nothing(monkeypatch, tmp_path, capsys, pairs, reason):
    store = FileSettings(tmp_path / "routing-settings.json")
    monkeypatch.setattr(balance_cli, "_routing_settings", lambda: store)

    assert balance_cli.cmd_balance_set(["codex-api-weight=40", *pairs], now=1.0) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"agentihooks balance set: {reason}\n"
    assert store.history() == []


def test_the_routing_store_is_named_redis_unless_it_is_a_file(tmp_path):
    assert balance_cli._store_label(object()) == "redis"
    assert balance_cli._store_label(FileSettings(tmp_path / "s.json")) == f"file {tmp_path / 's.json'}"


def test_routing_settings_open_the_store_through_the_routing_client(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    monkeypatch.setattr(place, "_client", lambda environ: seen.append(environ) or None)
    store = balance_cli._routing_settings()
    assert isinstance(store, FileSettings)
    assert store.path == tmp_path / "routing-settings.json"
    assert seen == [balance_cli.os.environ]


def test_the_balance_parser_takes_the_probe_flags_and_both_subcommands():
    parser = argparse.ArgumentParser()
    balance_cli.add_parser(parser.add_subparsers(dest="command"))
    plain = parser.parse_args(["balance"])
    assert (plain.balance_command, plain.fable, plain.refresh, plain.current, plain.timeout) == (
        None,
        False,
        False,
        False,
        60,
    )
    assert (plain.dry_run, plain.show_account_metadata) == (False, "")
    assert parser.parse_args(["balance", "--dry-run"]).dry_run is True
    assert parser.parse_args(["balance", "set", "a=1", "b=2"]).pairs == ["a=1", "b=2"]
    assert parser.parse_args(["balance", "settings"]).balance_command == "settings"
    with pytest.raises(SystemExit):
        parser.parse_args(["balance", "set"])


def test_a_value_keeps_every_equals_sign_after_the_first(monkeypatch, tmp_path, capsys):
    store = FileSettings(tmp_path / "routing-settings.json")
    monkeypatch.setattr(balance_cli, "_routing_settings", lambda: store)
    assert balance_cli.cmd_balance_set(["master-tier-claude=a=b"], now=1.0) == 0
    assert store.get("master-tier-claude") == "a=b"


def test_the_balance_help_names_every_flag_and_subcommand(monkeypatch, capsys):
    monkeypatch.setenv("COLUMNS", "300")
    monkeypatch.setenv("NO_COLOR", "1")
    parser = argparse.ArgumentParser(prog="agentihooks")
    balance_cli.add_parser(parser.add_subparsers(dest="command"))
    helps = []
    for argv in ([], ["balance"], ["balance", "set"], ["balance", "settings"]):
        with pytest.raises(SystemExit):
            parser.parse_args([*argv, "--help"])
        helps.append(capsys.readouterr().out)
    top, balance, set_help, settings_help = helps

    def shows(text, page):
        return re.search(rf"(?m)(?:^|\s){re.escape(text)}$", page) is not None

    assert shows("Probe and rank Claude OAuth accounts without launching workload", top)
    for text in (
        "Report routing state without launching Claude",
        "Include the separate Fable weekly quota",
        "Print every JSON event returned by a fresh probe for AH_CC_TOKEN_<SLUG>",
        "Ignore the 60-second quota cache",
        "Name the account this Claude session runs on; other accounts come from the quota cache",
        "Per-account probe timeout in seconds",
        "Write routing settings: set KEY=VALUE ... (VALUE none clears)",
        "List every routing setting key with its value",
    ):
        assert shows(text, balance), text
    for flag in ("--dry-run", "--fable", "--show-account-metadata SLUG", "--refresh", "--current", "--timeout TIMEOUT"):
        assert flag in balance
    assert "usage: agentihooks balance set [-h] KEY=VALUE [KEY=VALUE ...]" in set_help
    assert "usage: agentihooks balance settings [-h]" in settings_help
    timeout = parser.parse_args(["balance", "--timeout", "7"]).timeout
    assert (timeout, type(timeout)) == (7.0, float)


def test_run_sends_set_and_settings_to_their_commands(monkeypatch):
    calls = []
    monkeypatch.setattr(balance_cli, "cmd_balance_set", lambda pairs: calls.append(("set", pairs)) or 4)
    monkeypatch.setattr(balance_cli, "cmd_balance_settings", lambda: calls.append(("settings",)) or 5)
    probe = lambda **kwargs: calls.append(("probe", kwargs)) or 6  # noqa: E731
    assert balance_cli.run(argparse.Namespace(balance_command="set", pairs=["a=1"]), probe) == 4
    assert balance_cli.run(argparse.Namespace(balance_command="settings"), probe) == 5
    assert calls == [("set", ["a=1"]), ("settings",)]


def test_run_sends_a_plain_balance_to_the_probe_command():
    calls = []
    args = argparse.Namespace(
        balance_command=None, fable=True, refresh=False, timeout=9.0, show_account_metadata="B", current=False
    )
    assert balance_cli.run(args, lambda **kwargs: calls.append(kwargs) or 3) == 3
    assert calls == [
        {"include_fable": True, "refresh": False, "timeout": 9.0, "show_account_metadata": "B", "current": False}
    ]
