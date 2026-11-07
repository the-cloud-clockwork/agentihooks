"""A pass releases its tick lock even when closing the pass raises, so the next pass is not refused."""

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import pytest

from scripts.swarm import cli
from scripts.swarm.ledger_client import LedgerUnreachable
from tests.swarm.test_cli import env, run
from tests.swarm.test_delivery import FakeHerdr

__all__ = ["env"]

pytestmark = pytest.mark.unit


@pytest.mark.feature("swarm-tick-lock")
def test_a_pass_whose_closing_raises_still_releases_its_tick_lock(
    env: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")

    @contextmanager
    def failing_close(**_: Any) -> Generator[None, None, None]:
        yield
        raise LedgerUnreachable("the heartbeat timed out")

    monkeypatch.setattr(cli.pool, "running", failing_close)
    with pytest.raises(LedgerUnreachable, match="the heartbeat timed out"):
        cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert store.redis.get(store.key("sw", "tick-lock")) is None
    monkeypatch.undo()
    assert cli.run_tick(store, "sw", ledger, rt, FakeHerdr({})) != ["another tick is running"]
