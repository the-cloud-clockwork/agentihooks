from types import SimpleNamespace

import pytest

from scripts import herdr_gc, operator_env
from scripts.swarm import cli


@pytest.mark.parametrize("excluded", ["binned", "closed"])
def test_the_host_tick_excludes_retired_ledgers_before_any_tick_pass(monkeypatch, excluded):
    ticked, checked = [], []

    def binned(slug):
        checked.append(("binned", slug))
        return slug == "retired" and excluded == "binned"

    def closed(slug):
        checked.append(("closed", slug))
        assert not (slug == "retired" and excluded == "binned")
        return slug == "retired"

    ledger = SimpleNamespace(binned=binned, closed=closed)
    store = SimpleNamespace(slugs=lambda: ["retired", "active"])
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    monkeypatch.setattr(cli.timer, "installed_refusal", lambda: "")
    monkeypatch.setattr(cli, "_reconcile_services", lambda: None)
    monkeypatch.setattr(operator_env, "fill", lambda environ: None)
    monkeypatch.setattr(operator_env, "classifier", lambda environ: None)
    monkeypatch.setattr(herdr_gc, "run", lambda *args: [])

    def run_tick(received, slug, scheduled):
        assert received is store
        assert scheduled is True
        ticked.append(slug)
        return []

    monkeypatch.setattr(cli, "run_tick", run_tick)
    cli.cmd_tick(store, None)
    assert ticked == ["active"]
    assert checked == (
        [("binned", "retired"), ("binned", "active"), ("closed", "active")]
        if excluded == "binned"
        else [("binned", "retired"), ("closed", "retired"), ("binned", "active"), ("closed", "active")]
    )
