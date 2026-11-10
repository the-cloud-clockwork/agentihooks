"""The tick pass fences its runtime at its first site, before any step can spawn through it."""

from contextlib import ExitStack
from typing import Any, cast

import pytest

from scripts.swarm import cli, controller
from scripts.swarm.ledger_client import SNAPSHOT_TIMEOUT_S, LedgerClient
from scripts.tick.adapters.fakes import FakeRuntime, fake_tools
from scripts.tick.ports.tools import PASS_TOOLS

pytestmark = pytest.mark.unit


class _Store:
    def config(self, slug: str) -> str:
        return f"config of {slug}"


def _wrapper(state: dict[str, Any]) -> Any:
    wrapper: Any = object.__new__(cli._Wrapper)
    wrapper.held = "held"
    wrapper.beating = "beating"
    wrapper.state = state
    return wrapper


@pytest.mark.feature("tick-pass-fence-first-site")
def test_the_first_pass_site_fences_the_runtime_every_later_step_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(controller, "deployment_for", lambda config, environ: f"deployment for {config}")
    monkeypatch.setattr(controller, "FencedRuntime", lambda *args: pytest.fail("fenced outside the runtime port"))
    tools = fake_tools()
    port = cast(FakeRuntime, tools.runtime)
    port.answers["fence"] = "fenced"
    state: dict[str, Any] = {"store": _Store(), "slug": "sw", "runtime": "raw"}
    assert cli.PASS_SITES[0] == "wrapper probes"
    token = PASS_TOOLS.set(tools)
    try:
        assert _wrapper(state).hooks()[cli.PASS_SITES[0]]() is True
    finally:
        PASS_TOOLS.reset(token)
    assert state["runtime"] == "fenced"
    assert [(call.operation, call.args) for call in port.calls] == [("fence", ("held", "deployment for config of sw"))]
    assert port.calls[0].deadline.remaining > 0


@pytest.mark.feature("tick-pass-fence-first-site")
def test_the_health_site_holds_the_ledger_view_without_fencing_again(monkeypatch: pytest.MonkeyPatch) -> None:
    starts: list[tuple[str, bool, float | None]] = []
    monkeypatch.setattr(controller, "FencedRuntime", lambda *args: pytest.fail("fenced twice"))
    monkeypatch.setattr(cli.ledger_view, "reads", lambda ledger: True)
    monkeypatch.setattr(
        cli.ledger_view, "start", lambda slug, service, timeout=None: starts.append((slug, service, timeout))
    )
    state: dict[str, Any] = {"store": _Store(), "slug": "sw", "runtime": "raw", "ledger": "ledger"}
    with ExitStack() as beating:
        cli._hold_view(beating, "sw", LedgerClient(service=True))
    assert starts == [("sw", True, SNAPSHOT_TIMEOUT_S)]
    assert "wrapper health" not in _wrapper(state).hooks()
    assert state["runtime"] == "raw"
