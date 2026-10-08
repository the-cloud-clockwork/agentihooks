import sys

import pytest

from scripts.swarm_ledger import ledger


@pytest.fixture
def run(monkeypatch, capsys):
    for key in ("AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_SWARM"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(ledger, "call", lambda slug: {"title": "Café", "slug": slug})
    monkeypatch.setattr(ledger, "cmd_url", lambda args: print(f"url {args.slug}"))

    def go(*argv):
        monkeypatch.setattr(sys, "argv", ["ledger", *argv])
        ledger.main()
        return capsys.readouterr().out

    return go


def test_show_prints_the_stored_ledger_as_indented_unescaped_json(run):
    assert run("--slug", "demo", "show") == '{\n "title": "Café",\n "slug": "demo"\n}\n'


def test_url_and_show_need_only_the_slug(run):
    assert run("--slug", "demo", "url") == "url demo\n"


@pytest.mark.parametrize("argv", [("--as", "engineer@a1-1", "show"), ("--slug", "demo", "status")])
def test_a_missing_slug_or_a_member_command_without_a_name_is_refused(run, argv):
    with pytest.raises(SystemExit, match="--slug and --as are required"):
        run(*argv)
