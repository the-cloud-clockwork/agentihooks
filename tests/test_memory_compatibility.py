"""Tests for SV2-MIG-01 search consumer inventory and extraction boundary."""

import hashlib
import json
from pathlib import Path

import pytest

from hooks.memory import compatibility as compat
from hooks.memory.api.ingest import Rejected
from hooks.memory.api.records import ALIASES
from tests import sv2_mig01_cases as cases

pytestmark = pytest.mark.unit
EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-MIG-01"
SEARCH = {"consumer": "search caller", "kind": "tool", "uses": "search_semantic"}


def rejected(call) -> str:
    with pytest.raises(Rejected) as error:
        call()
    return error.value.error_class


@pytest.mark.parametrize("name", sorted(ALIASES))
def test_aliased_tools_classify_as_alias_to_their_session_tool(name):
    entry = {"consumer": name, "kind": "tool", "uses": name}
    assert compat.classify(entry) == {**entry, "decision": "alias", "target": ALIASES[name]}


def test_a_replaced_capability_names_its_agentihooks_target():
    entry = {"consumer": "landing", "kind": "dashboard", "uses": "/sessions"}
    assert compat.classify(entry) == {**entry, "decision": "replace", "target": "session_list"}


@pytest.mark.parametrize(
    ("use", "feature"),
    [("dispatch_task", "dispatcher"), ("/agents", "agent_registry"), ("/health", "standalone_daemon")],
)
def test_a_retired_capability_is_removed_with_its_feature(use, feature):
    entry = {"consumer": "caller", "kind": "tool", "uses": use}
    assert compat.classify(entry) == {**entry, "decision": "remove", "retired": feature}


def test_an_unknown_capability_stays_unmapped():
    entry = {"consumer": "export", "kind": "ingestion", "uses": "export_sessions"}
    assert compat.classify(entry) == {**entry, "decision": None}


@pytest.mark.parametrize(
    "entry",
    [
        None,
        ["consumer"],
        {"consumer": "c", "kind": "tool"},
        {**SEARCH, "decision": "alias"},
        {**SEARCH, "consumer": ""},
        {**SEARCH, "uses": 3},
        {**SEARCH, "kind": "pager"},
    ],
)
def test_a_malformed_entry_is_refused(entry):
    assert rejected(lambda: compat.classify(entry)) == "bad_consumer"


def test_every_known_consumer_has_a_target_or_a_removal():
    report = cases.known()
    assert report[compat.UNMAPPED] == 0
    assert report["consumers"] == len({entry["consumer"] for entry in compat.CONSUMERS})
    assert report["kinds"] == ["brain_extraction", "dashboard", "deployment", "ingestion", "tool"]


def test_every_legacy_tool_is_in_the_inventory():
    tools = {entry["uses"] for entry in compat.CONSUMERS if entry["kind"] == "tool"}
    assert set(ALIASES) <= tools
    assert {"dispatch_task", "register_agent", "restore_session", "get_session_actions"} <= tools
    assert not tools & (compat.NOT_TOOLS | {"mcp_tool_grant", "postgres_history"})
    assert not any(tool.startswith("/") for tool in tools)


@pytest.mark.parametrize("component", sorted(compat.EXTRACTABLE))
def test_search_components_extract_without_copying(component):
    assert compat.extract(component, "module") == {
        "component": component,
        "source_module": "module",
        "target": compat.EXTRACTABLE[component],
        "source_license": "MIT",
        "copied": False,
    }


@pytest.mark.parametrize(
    ("component", "error"),
    [("dispatcher", "retired_feature"), ("run_agent", "retired_feature"), ("ui", "not_extractable")],
)
def test_retired_and_unknown_components_are_not_extracted(component, error):
    assert rejected(lambda: compat.extract(component, "module")) == error


def test_add_records_a_consumer_and_moves_the_revision():
    inventory = compat.Inventory()
    assert inventory.add(SEARCH, 1) == {"item": compat.classify(SEARCH), "replayed": False}
    assert inventory.state == {"revision": 1, "items": {"search caller": compat.classify(SEARCH)}}


def test_a_replayed_add_changes_nothing_at_any_revision():
    inventory = compat.Inventory()
    inventory.add(SEARCH, 2)
    assert inventory.add(SEARCH, 1) == {"item": compat.classify(SEARCH), "replayed": True}
    assert inventory.state["revision"] == 2


@pytest.mark.parametrize("revision", [1, 2])
def test_a_stale_revision_cannot_overwrite_an_item(revision):
    inventory = compat.Inventory()
    inventory.add(SEARCH, 2)
    before = inventory.dumps()
    assert rejected(lambda: inventory.add({**SEARCH, "uses": "dispatch_task"}, revision)) == "stale_revision"
    assert inventory.dumps() == before


def test_report_counts_unmapped_consumers_in_name_order():
    inventory = compat.Inventory()
    inventory.add({"consumer": "b", "kind": "cli", "uses": "nothing"}, 1)
    inventory.add({**SEARCH, "consumer": "a"}, 2)
    report = inventory.report()
    assert [item["consumer"] for item in report["items"]] == ["a", "b"]
    assert (report["revision"], report[compat.UNMAPPED], report["unmapped"]) == (2, 1, ["b"])


def test_loads_restores_dumps_without_sharing_state():
    state = {"revision": 1, "items": {"search caller": compat.classify(SEARCH)}}
    inventory = compat.Inventory(state)
    inventory.add({**SEARCH, "consumer": "other"}, 2)
    assert state["revision"] == 1
    assert compat.Inventory.loads(inventory.dumps()).state == inventory.state


@pytest.mark.parametrize("case", ["A", "B", "C"])
def test_package_cases_match_committed_evidence(tmp_path, case):
    observed = json.loads(json.dumps(cases.run_case(case, tmp_path)))
    path = EVIDENCE / f"{case.lower()}-result.json"
    expected = json.loads(path.read_text()) if path.exists() else None
    assert observed == expected, json.dumps(observed, sort_keys=True)


def test_package_record_and_manifest_match_the_fixture_inputs():
    record = json.loads((EVIDENCE / "result.json").read_text())
    found = json.loads((EVIDENCE / "manifest.json").read_text())
    inputs = (cases.FIXTURE, cases.CASES_MODULE)
    assert found["input_sha256"] == {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs}
    assert found["fixture_inputs"] == record["fixture_inputs"] == cases.FIXTURE_INPUTS
    assert found["tested_commit"] == record["tested_commit"]
    assert record["evidence_class"] == cases.EVIDENCE_CLASS
