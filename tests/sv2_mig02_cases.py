import json
from pathlib import Path

from hooks.memory import source_audit as audit
from hooks.memory.api.ingest import Rejected

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "sv2_mig02"
FIXTURE = FIXTURE_DIR / "fixture.json"
RAW_FILES = (FIXTURE_DIR / "alpha.jsonl", FIXTURE_DIR / "late" / "beta.jsonl")
CASES_MODULE = Path(__file__)
FIXTURE_INPUTS = {
    "complete_jsonl": "alpha, a surviving raw transcript with its legacy preview and vector rows",
    "capped_redis_preview": "beta, a legacy Redis entry preview with one entry at the 500 character cap",
    "vector_only_record": "gamma, legacy vector rows with no raw transcript and no preview",
    "missing_source": "delta, a session the legacy index named with no surviving material",
}
EVIDENCE_CLASS = (
    "mocked: the legacy Redis previews and vector rows are fixture data shaped like the retired transcript service's "
    "store and embedding code, and a JSON file stands in for the authoritative audit record; no legacy Redis, "
    "Postgres or service was read"
)


def fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def observed(source: dict) -> dict:
    raw = source.get("raw")
    return {**source, "raw": (FIXTURE_DIR / raw).read_bytes() if raw else None}


def refused(call) -> dict:
    try:
        call()
    except Rejected as error:
        return {"error_class": error.error_class, "detail": error.detail}
    raise AssertionError("the call was not refused")


def built(doc: dict) -> audit.SourceAudit:
    held = audit.SourceAudit()
    held.run([observed(source) for source in doc["sources"]], 1)
    return held


def stored(root: Path, doc: dict) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    record = root / "audit.json"
    held = built(doc)
    record.write_text(held.dumps(), encoding="utf-8")
    read_back = audit.SourceAudit.loads(record.read_text(encoding="utf-8"))
    return {"report": read_back.report(), "matches_memory": read_back.state == held.state}


def positive(root: Path) -> dict:
    doc = fixture()
    first, second = stored(root / "first", doc), stored(root / "second", doc)
    return {"case": "A", "first": first, "repeat_matches": first == second}


def rejection(root: Path) -> dict:
    doc = fixture()
    held = built(doc)
    before = held.dumps()
    refusals = {
        "bad_sources": [refused(lambda bad=bad: held.run([bad], 2)) for bad in doc["bad"]],
        "forged_hash": refused(lambda: held.run([observed(doc["forged_hash"])], 2)),
        "lossless_label": {name: refused(lambda name=name: held.certify(name)) for name in ("beta", "gamma", "delta")},
        "unknown_source": refused(lambda: held.certify("epsilon")),
        "stale_upgrade": refused(lambda: held.run([observed(doc["late"])], 1)),
    }
    return {
        "case": "B",
        "refusals": refusals,
        "refusals_left_state_unchanged": held.dumps() == before,
        "refusals_disclose_the_input": doc["private_label"] in json.dumps(refusals),
        "full_raw_label": held.certify("alpha"),
        "report": held.report(),
    }


def recovery(root: Path) -> dict:
    doc = fixture()
    root.mkdir(parents=True, exist_ok=True)
    record = root / "audit.json"
    record.write_text(built(doc).dumps(), encoding="utf-8")
    interrupted = audit.SourceAudit.loads(record.read_text(encoding="utf-8"))
    interrupted.run([observed(doc["late"])], 2)
    restarted = audit.SourceAudit.loads(record.read_text(encoding="utf-8"))
    survived = restarted.report()[audit.METRIC]
    rerun = [observed(source) for source in doc["sources"][:1]] + [observed(doc["late"])]
    rerun += [observed(source) for source in doc["sources"][2:]]
    upgraded = restarted.run(rerun, 2)
    record.write_text(restarted.dumps(), encoding="utf-8")
    replayed = audit.SourceAudit.loads(record.read_text(encoding="utf-8")).run(rerun, 2)
    older = {**doc["sources"][0], "vectors": None}
    stale = refused(lambda: restarted.run([observed(older)], 2))
    raw_gone = restarted.run([observed(source) for source in doc["sources"]], 3)
    return {
        "case": "C",
        "survived_restart": survived,
        "upgraded": upgraded,
        "replayed": replayed,
        "stale_overwrite": stale,
        "raw_gone_again": raw_gone,
        "beta_label": restarted.certify("beta"),
        "report": restarted.report(),
    }


def run_case(case: str, root: Path) -> dict:
    return {"A": positive, "B": rejection, "C": recovery}[case](root)
