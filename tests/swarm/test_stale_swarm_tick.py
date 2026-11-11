"""A swarm whose ledger is closed or binned leaves the controller tick, and a web address is refused as a swarm repo."""

from typing import Any

import pytest

from scripts.swarm import cli, controller
from scripts.swarm.ledger_client import LedgerGone
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from tests.swarm.test_cli import env, run  # noqa: F401

pytestmark = pytest.mark.unit


class _Ledger:
    """A ledger answering which swarms are closed, binned or gone."""

    def __init__(self, closed: set[str], binned: set[str], gone: set[str]) -> None:
        self.closed_slugs, self.binned_slugs, self.gone = closed, binned, gone

    def binned(self, slug: str) -> bool:
        if slug in self.gone:
            raise LedgerGone(f"ledger {slug} does not exist")
        return slug in self.binned_slugs

    def closed(self, slug: str) -> bool:
        return slug in self.closed_slugs


@pytest.mark.feature("controller-skips-closed-swarms")
def test_the_controller_runs_no_steps_for_a_closed_or_binned_swarm_holding_a_ghost_agent(
    env: tuple[RedisStore, Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _, _ = env
    for slug in ("proof", "binned", "live", "gone"):
        store.create(SwarmConfig(slug, "/repo", 1, 0, state="stopping" if slug == "proof" else "running"))
    store.put_agent("proof", AgentRecord("engineer@dead-0001", "eng", "t1", state="suspect", seat="eng-1@proof"))
    ticks: list[str] = []
    monkeypatch.delenv("AGENTIHOOKS_CONTROL_HOST_SWARMS", raising=False)
    monkeypatch.setattr(cli, "run_tick", lambda saved, slug, *args, **kwargs: ticks.append(slug) or [slug])

    ledger = _Ledger({"proof"}, {"binned"}, {"gone"})

    assert controller.run_once(store, ledger) == {"gone": ["gone"], "live": ["live"]}
    assert ticks == ["gone", "live"]
    assert store.redis.get(store.key("proof", "controller-host")) is None


@pytest.mark.feature("controller-skips-closed-swarms")
def test_tickable_reads_binned_then_closed_and_ticks_a_gone_ledger() -> None:
    ledger = _Ledger({"closed"}, {"binned"}, {"gone"})

    assert [controller.tickable(ledger, slug) for slug in ("closed", "binned", "gone", "open")] == [
        False,
        False,
        True,
        True,
    ]


@pytest.mark.feature("swarm-create-refuses-web-repo")
@pytest.mark.parametrize(
    "repo",
    [
        "https://github.com/team/project",
        "git@github.com:team/project.git",
        "github.com/team/project",
        "ssh://git@github.com/team/project",
    ],
)
def test_create_refuses_a_web_address_as_its_repo(
    env: tuple[RedisStore, Any, Any],  # noqa: F811
    capsys: pytest.CaptureFixture[str],
    repo: str,
) -> None:
    store, _, _ = env

    assert run("sw", "create", "--repo", repo) == 1

    assert capsys.readouterr().err.strip() == (f"swarm: --repo takes a folder on this host, not the web address {repo}")
    assert store.slugs() == []


@pytest.mark.feature("swarm-create-refuses-web-repo")
@pytest.mark.parametrize(
    "repo", ["/missing/checkout", "relative/checkout", "~/dev/project", "my.project/checkout", "v1.2/app"]
)
def test_create_keeps_a_local_path_even_when_its_folder_is_absent(
    env: tuple[RedisStore, Any, Any],  # noqa: F811
    repo: str,
) -> None:
    store, _, _ = env

    assert run("sw", "create", "--repo", repo) == 0

    assert store.slugs() == ["sw"]
