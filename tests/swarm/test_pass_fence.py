"""The tick pass fences its runtime at its first site, before any step can spawn through it."""

from typing import Any

import pytest

from scripts.swarm import cli, controller

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
    made: list[tuple[Any, ...]] = []
    monkeypatch.setattr(controller, "deployment_for", lambda config, environ: f"deployment for {config}")
    monkeypatch.setattr(controller, "FencedRuntime", lambda *args: made.append(args) or "fenced")
    store = _Store()
    state: dict[str, Any] = {"store": store, "slug": "sw", "runtime": "raw"}
    assert cli.PASS_SITES[0] == "wrapper probes"
    assert _wrapper(state).hooks()[cli.PASS_SITES[0]]() is True
    assert state["runtime"] == "fenced"
    assert made == [(store, "sw", "held", "raw", "deployment for config of sw")]


@pytest.mark.feature("tick-pass-fence-first-site")
def test_the_health_site_holds_the_ledger_view_without_fencing_again(monkeypatch: pytest.MonkeyPatch) -> None:
    held: list[tuple[Any, ...]] = []
    monkeypatch.setattr(cli, "_hold_view", lambda beating, slug, ledger: held.append((beating, slug, ledger)))
    monkeypatch.setattr(controller, "FencedRuntime", lambda *args: pytest.fail("fenced twice"))
    state: dict[str, Any] = {"store": _Store(), "slug": "sw", "runtime": "raw", "ledger": "ledger"}
    assert _wrapper(state).hooks()["wrapper health"]() is True
    assert held == [("beating", "sw", "ledger")]
    assert state["runtime"] == "raw"
