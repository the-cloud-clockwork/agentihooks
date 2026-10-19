from threading import Event
from types import SimpleNamespace

import pytest

from scripts import herdr_gc, operator_env
from scripts.swarm import cli


@pytest.mark.parametrize("excluded", ["binned", "closed", "unavailable", "slow"])
def test_the_host_tick_excludes_retired_ledgers_before_any_tick_pass(monkeypatch, excluded, capsys):
    ticked, checked = [], []
    active_ticked = Event()

    def binned(slug):
        checked.append(("binned", slug))
        return slug == "retired" and excluded == "binned"

    def closed(slug):
        checked.append(("closed", slug))
        assert not (slug == "retired" and excluded == "binned")
        if slug == "retired" and excluded == "unavailable":
            raise RuntimeError("ledger unavailable")
        if slug == "retired" and excluded == "slow":
            assert active_ticked.wait(5)
        return slug == "retired"

    ledger = SimpleNamespace(binned=binned, closed=closed)
    store = SimpleNamespace(
        slugs=lambda: ["retired", "active"],
        config=lambda slug: SimpleNamespace(api_url="", pending_api_url=""),
        redis=SimpleNamespace(hget=lambda *_: None),
        key=lambda slug, tail: f"{slug}:{tail}",
    )
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    monkeypatch.setattr(cli.timer, "installed_refusal", lambda: "")
    monkeypatch.setattr(cli, "_reconcile_services", lambda: None)
    monkeypatch.setattr(operator_env, "fill", lambda environ: None)
    monkeypatch.setattr(operator_env, "classifier", lambda environ: None)
    monkeypatch.setattr(herdr_gc, "run", lambda *args, **kwargs: [])

    def run_tick(received, slug, scheduled):
        assert received is store
        assert scheduled is True
        ticked.append(slug)
        active_ticked.set()
        return []

    monkeypatch.setattr(cli, "run_tick", run_tick)
    cli.cmd_tick(store, None)
    assert ticked == ["active"]
    error = capsys.readouterr().err
    if excluded == "unavailable":
        assert "retired: RuntimeError: ledger unavailable" in error
    else:
        assert error == ""
    assert sorted(checked) == sorted(
        [("binned", "retired"), ("binned", "active"), ("closed", "active")]
        if excluded == "binned"
        else [("binned", "retired"), ("closed", "retired"), ("binned", "active"), ("closed", "active")]
    )


@pytest.mark.parametrize("missing_at", ["binned", "closed"])
def test_missing_ledgers_reach_the_existing_tick_diagnostic(monkeypatch, missing_at):
    checked = []

    def check(stage, slug):
        assert slug == "missing"
        checked.append(stage)
        if stage == missing_at:
            raise cli.LedgerGone("ledger missing does not exist")
        return False

    ledger = SimpleNamespace(binned=lambda slug: check("binned", slug), closed=lambda slug: check("closed", slug))
    monkeypatch.setattr(cli, "LedgerClient", lambda: ledger)
    assert cli._tickable("missing") is True
    assert checked == (["binned"] if missing_at == "binned" else ["binned", "closed"])
