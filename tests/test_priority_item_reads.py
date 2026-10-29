import importlib.util
from pathlib import Path
from unittest.mock import Mock, call
from urllib.error import HTTPError

import pytest

from scripts.swarm_ledger import ledger

SCRIPT = Path(__file__).resolve().parents[1] / "profiles/package/skills/triage-priorities/scripts/list_priorities.py"


def helper():
    spec = importlib.util.spec_from_file_location("priority_item_reads", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_triage_reads_the_complete_priorities_collection_and_only_referenced_items(monkeypatch):
    priorities = [{"id": "one", "item": "tasks/t1"}, {"id": "two", "item": "tasks/t1"}]
    task = {"id": "t1", "title": "Work", "comments": [{"text": "context"}]}
    resource = Mock(side_effect=[priorities, task])
    monkeypatch.setattr(ledger, "resource", resource)
    assert helper().load("sw") == {"priorities": priorities, "tasks": [task]}
    assert resource.call_args_list == [call("sw", "priorities", collection=True), call("sw", "tasks/t1")]


def test_a_missing_referenced_item_remains_resolvable(monkeypatch):
    priorities = [{"id": "one", "item": "tasks/gone"}]
    monkeypatch.setattr(ledger, "resource", Mock(side_effect=[priorities, HTTPError("url", 404, "missing", {}, None)]))
    module = helper()
    assert module.triage(module.load("sw")) == {
        "open": [],
        "resolved": [{"priority": "one", "item": "tasks/gone", "why": "item missing"}],
    }


def test_a_failed_item_read_does_not_clear_a_priority(monkeypatch):
    priorities = [{"id": "one", "item": "tasks/t1"}]
    monkeypatch.setattr(
        ledger, "resource", Mock(side_effect=[priorities, HTTPError("url", 503, "unavailable", {}, None)])
    )
    with pytest.raises(SystemExit, match="cannot read ledger sw"):
        helper().load("sw")
