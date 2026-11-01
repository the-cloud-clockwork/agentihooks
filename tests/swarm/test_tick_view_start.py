import pytest

from scripts.swarm import cli, controller
from scripts.tick import view
from tests.swarm.memory_fixture import tmp_path
from tests.swarm.test_cli import env, run
from tests.swarm.test_delivery import FakeHerdr

__all__ = ["env", "tmp_path"]


@pytest.mark.feature("swarm-tick-delta-reads")
def test_a_pass_starts_the_ledger_view_once_before_its_first_ledger_read(
    env: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _, rt = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    monkeypatch.setattr(cli.outage, "ledger_health", lambda slug, at, probe: True)
    client = cli.ledger_client.LedgerClient(service=True)
    started, reads = [], []
    monkeypatch.setattr(client, "state", lambda slug: reads.append(slug) or {})

    def start(slug: str, service: bool = False) -> None:
        started.append((slug, service, list(reads)))
        raise RuntimeError("view read")

    monkeypatch.setattr(view, "start", start)
    with pytest.raises(RuntimeError, match="^view read$"):
        cli.run_tick(store, "sw", client, rt, FakeHerdr({}))
    assert started == [("sw", True, [])]
    assert store.redis.get(store.key("sw", "tick-lock")) is None


def test_a_pass_with_a_stand_in_ledger_reads_no_view(env: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    started = []
    monkeypatch.setattr(view, "start", lambda slug, service=False: started.append(slug))
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert started == []


def test_a_pass_with_a_ledger_client_stand_in_reads_no_view(env: tuple, monkeypatch: pytest.MonkeyPatch) -> None:
    store, _, rt = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    monkeypatch.setattr(cli.outage, "ledger_health", lambda slug, at, probe: True)

    class StandIn(cli.ledger_client.LedgerClient):
        """A ledger client subclass, as the test suites stand in for the ledger."""

    def fenced(*args: object) -> None:
        raise RuntimeError("fenced")

    started = []
    monkeypatch.setattr(view, "start", lambda slug, service=False: started.append(slug))
    monkeypatch.setattr(controller, "FencedRuntime", fenced)
    with pytest.raises(RuntimeError, match="^fenced$"):
        cli.run_tick(store, "sw", StandIn(), rt, FakeHerdr({}))
    assert started == []
