import contextlib
import sys

import pytest

from scripts import operator_env
from scripts.swarm import cli, tick_stop
from scripts.swarm_ledger import authority_context

pytestmark = pytest.mark.unit


def test_the_host_tick_shortens_the_thread_switch_interval_before_any_swarm_ticks(monkeypatch):
    seen = []
    monkeypatch.delenv("AGENTIHOOKS_CONTROLLER_TICK_SECONDS", raising=False)
    monkeypatch.setattr(sys, "setswitchinterval", lambda seconds: seen.append(("switch", seconds)))
    monkeypatch.setattr(operator_env, "fill", lambda environ: None)
    monkeypatch.setattr(operator_env, "classifier", lambda environ: None)
    monkeypatch.setattr(authority_context, "for_tick", lambda store: contextlib.nullcontext(["one", "two"]))
    monkeypatch.setattr(cli, "_reconcile_services", lambda: None)
    monkeypatch.setattr(tick_stop, "run", lambda slugs, *rest: seen.append(("tick", slugs)) or True)

    cli._tick_host(object(), 0.0)

    assert seen == [("switch", 0.0005), ("tick", ["one", "two"])]
