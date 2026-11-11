"""Tick modules first imported under a patched inbox store still reach the real store class once the patch ends."""

import importlib
import sys
from types import ModuleType
from unittest.mock import patch

import fakeredis
import pytest

import scripts.swarm
from scripts.inbox import store as inbox_store
from scripts.swarm import run_window
from scripts.swarm.store import RedisStore

MODULES = ["preempt", "run_window", "dispatch_seat", "ledger_events"]


@pytest.mark.feature("tick-inbox-store-lookup")
@pytest.mark.parametrize("name", MODULES)
def test_a_first_import_under_a_patched_inbox_store_keeps_no_fake(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    module: ModuleType = importlib.import_module(f"scripts.swarm.{name}")
    real = inbox_store.InboxStore
    monkeypatch.setattr(scripts.swarm, name, module)
    monkeypatch.delitem(sys.modules, module.__name__)
    with patch("scripts.inbox.store.InboxStore") as fake:
        fresh = importlib.import_module(module.__name__)
    assert [key for key, value in vars(fresh).items() if value is fake] == []
    assert getattr(fresh, "inbox_store", inbox_store).InboxStore is real


@pytest.mark.feature("tick-inbox-store-lookup")
def test_the_run_window_sends_through_the_real_store_after_a_patched_first_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(scripts.swarm, "run_window", run_window)
    monkeypatch.delitem(sys.modules, run_window.__name__)
    with patch("scripts.inbox.store.InboxStore"):
        fresh = importlib.import_module(run_window.__name__)
    fresh._send_once(store, "master@sw", "window closes", "sw:window:1")
    assert [item.text for item in inbox_store.InboxStore(store.redis).inbox("master@sw")] == ["window closes"]
