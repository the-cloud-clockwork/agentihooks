import json
from collections import Counter
from pathlib import Path

from hooks.memory import compatibility as compat
from hooks.memory.api.ingest import Rejected

FIXTURE = Path(__file__).parent / "fixtures" / "sv2_mig01" / "fixture.json"
CASES_MODULE = Path(__file__)
FIXTURE_INPUTS = {
    "search_caller": "an agent memory call to the legacy semantic search tool",
    "obsolete_dispatcher": "a workforce caller of the legacy task dispatch tool",
    "dashboard_metric_reader": "the landing sessions indexed figure read from the legacy sessions endpoint",
}
EVIDENCE_CLASS = (
    "mocked: the consumer inventory is a fixture and a JSON file stands in for the authoritative migration record; "
    "the known consumer list was read from antoncore and agentibrain-kernel source, and no legacy service, gateway "
    "or dashboard was called"
)


def fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def refused(call) -> dict:
    try:
        call()
    except Rejected as error:
        return {"error_class": error.error_class, "detail": error.detail}
    raise AssertionError("the call was not refused")


def built(doc: dict) -> compat.Inventory:
    inventory = compat.Inventory()
    for revision, entry in enumerate(doc["consumers"], start=1):
        inventory.add(entry, revision)
    return inventory


def known() -> dict:
    inventory = compat.Inventory()
    for revision, entry in enumerate(compat.CONSUMERS, start=1):
        inventory.add(entry, revision)
    report = inventory.report()
    return {
        "consumers": len(report["items"]),
        compat.UNMAPPED: report[compat.UNMAPPED],
        "decisions": dict(sorted(Counter(item["decision"] for item in report["items"]).items())),
        "kinds": sorted({item["kind"] for item in report["items"]}),
    }


def stored(root: Path, doc: dict) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    record = root / "inventory.json"
    record.write_text(built(doc).dumps(), encoding="utf-8")
    return compat.Inventory.loads(record.read_text(encoding="utf-8")).report()


def positive(root: Path) -> dict:
    doc = fixture()
    first, second = stored(root / "first", doc), stored(root / "second", doc)
    extracted = [compat.extract(name, doc["source_module"]) for name in doc["extractable"]]
    return {"case": "A", "first": first, "repeat_matches": first == second, "extracted": extracted, "known": known()}


def rejection(root: Path) -> dict:
    doc = fixture()
    inventory = built(doc)
    before = inventory.dumps()
    refusals = {
        "extract": {
            name: refused(lambda name=name: compat.extract(name, doc["source_module"])) for name in doc["retired"]
        },
        "bad_entries": [refused(lambda entry=entry: inventory.add(entry, 9)) for entry in doc["bad"]],
        "stale_reclassify": refused(lambda: inventory.add(doc["reclassify"], 3)),
    }
    unchanged = inventory.dumps() == before
    disclosed = doc["private_label"] in json.dumps(refusals)
    unknown = inventory.add(doc["unknown"], 4)
    return {
        "case": "B",
        "refusals": refusals,
        "refusals_left_state_unchanged": unchanged,
        "refusals_disclose_the_entry": disclosed,
        "dispatcher": inventory.state["items"][doc["consumers"][1]["consumer"]],
        "unknown": unknown,
        "report": inventory.report(),
    }


def recovery(root: Path) -> dict:
    doc = fixture()
    root.mkdir(parents=True, exist_ok=True)
    record = root / "inventory.json"
    record.write_text(built(doc).dumps(), encoding="utf-8")
    interrupted = compat.Inventory.loads(record.read_text(encoding="utf-8"))
    interrupted.add(doc["late"], 4)
    restarted = compat.Inventory.loads(record.read_text(encoding="utf-8"))
    survived = sorted(restarted.state["items"])
    added = restarted.add(doc["late"], 4)
    record.write_text(restarted.dumps(), encoding="utf-8")
    replay = compat.Inventory.loads(record.read_text(encoding="utf-8")).add(doc["late"], 4)
    stale = refused(lambda: restarted.add(doc["late_conflict"], 4))
    report = restarted.report()
    return {
        "case": "C",
        "survived_restart": survived,
        "late_added": added,
        "late_replayed": replay,
        "stale_overwrite": stale,
        "late_after_stale": restarted.state["items"][doc["late"]["consumer"]],
        "report": report,
    }


def run_case(case: str, root: Path) -> dict:
    return {"A": positive, "B": rejection, "C": recovery}[case](root)
