"""ledger and swarm refuse an --as name other than the swarm session's own agent name."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.swarm import cli as swarm_cli
from scripts.swarm_ledger import ledger

ME, OTHER, SLUG = "engineer@1-1", "engineer@1-2", "demo"
REFUSED = f"this session is {ME} in swarm {SLUG} and cannot act as {OTHER}: run the command with --as {ME}"


@pytest.fixture(autouse=True)
def no_redis():
    with patch("hooks._redis.get_redis", return_value=None):
        yield


@pytest.fixture
def in_swarm(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_SWARM", SLUG)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", ME)


@pytest.fixture
def outside_swarm(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", ME)


def run_ledger(monkeypatch, name):
    seen = []
    monkeypatch.setattr("sys.argv", ["agentihooks ledger", "--slug", SLUG, "--as", name, "status"])
    monkeypatch.setattr(ledger, "cmd_status", seen.append)
    ledger.main()
    return seen


class TestLedger:
    def test_refuses_another_agents_name(self, in_swarm, monkeypatch):
        with pytest.raises(SystemExit) as exc:
            run_ledger(monkeypatch, OTHER)
        assert exc.value.code == f"agentihooks ledger: {REFUSED}"

    def test_runs_with_own_name(self, in_swarm, monkeypatch):
        assert [args.name for args in run_ledger(monkeypatch, ME)] == [ME]

    def test_runs_any_name_outside_a_swarm(self, outside_swarm, monkeypatch):
        assert [args.name for args in run_ledger(monkeypatch, OTHER)] == [OTHER]


def run_swarm(monkeypatch, argv):
    seen = []
    monkeypatch.setattr(
        swarm_cli, "connect", lambda: SimpleNamespace(names=SimpleNamespace(swarm_slug=lambda slug: slug))
    )
    monkeypatch.setattr(swarm_cli, "cmd_status", lambda store, args: seen.append(args.name))
    return swarm_cli.main(argv), seen


class TestSwarm:
    def test_refuses_another_agents_name(self, in_swarm, monkeypatch, capsys):
        assert run_swarm(monkeypatch, [SLUG, "--as", OTHER, "status"]) == (1, [])
        assert capsys.readouterr().err == f"swarm: {REFUSED}\n"

    def test_runs_with_own_name(self, in_swarm, monkeypatch):
        assert run_swarm(monkeypatch, [SLUG, "--as", ME, "status"]) == (0, [ME])

    def test_runs_without_a_name(self, in_swarm, monkeypatch):
        assert run_swarm(monkeypatch, [SLUG, "status"]) == (0, [""])

    def test_runs_any_name_outside_a_swarm(self, outside_swarm, monkeypatch):
        assert run_swarm(monkeypatch, [SLUG, "--as", OTHER, "status"]) == (0, [OTHER])

    def test_commands_without_a_slug_still_run(self, in_swarm, monkeypatch):
        seen = []
        monkeypatch.setattr(swarm_cli, "connect", lambda: "store")
        monkeypatch.setattr(swarm_cli, "cmd_templates", lambda store, args: seen.append(args))
        assert swarm_cli.main(["templates"]) == 0
        assert len(seen) == 1
