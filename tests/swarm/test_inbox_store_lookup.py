"""Tick modules first imported under a patched inbox store still reach the real store class once the patch ends."""

import importlib
import sys
from types import ModuleType
from unittest.mock import patch

import pytest

import scripts.swarm
from scripts.inbox import store as inbox_store

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
    assert inbox_store.InboxStore is real
