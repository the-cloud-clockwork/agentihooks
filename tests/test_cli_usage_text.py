import sys

import pytest

from scripts import install


def _help(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["agentihooks", "-h"])
    with pytest.raises(SystemExit) as raised:
        install.main()
    assert raised.value.code == 0
    return capsys.readouterr().out


def test_the_help_lists_the_token_commands_and_the_slug_rule(monkeypatch, capsys):
    text = _help(monkeypatch, capsys)
    assert "agentihooks codex login <slug> [--device-auth] [--openbao|--no-openbao]" in text
    assert "agentihooks codex refresh [--force SLUG] [--days N]" in text
    assert "agentihooks claude login [slug] [--openbao|--no-openbao]" in text
    assert "secret/agentihooks/codex-creds" in text and "secret/agentihooks/claude-creds" in text
    assert "use the account email; lowercase, @ becomes at, every other symbol becomes _" in text


def test_the_codex_quota_and_balance_lines_describe_today(monkeypatch, capsys):
    text = _help(monkeypatch, capsys)
    assert "Route to an AH_CX_TOKEN account (the default login only when none is set) and launch Codex" in text
    assert "One table for every Claude and Codex account" in text
    assert "Route to the default login or an AH_CX_TOKEN account" not in text
