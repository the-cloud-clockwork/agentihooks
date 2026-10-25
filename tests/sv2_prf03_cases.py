import hashlib
import json
import tempfile
from collections import Counter
from dataclasses import replace
from pathlib import Path

from hooks.memory.catalog import Conflict
from tests.performance.session_memory import load
from tests.performance.session_memory.load import Run, Variant

EVIDENCE_CLASS = (
    "mocked: interleaved writers in one process, a SQLite catalog file, a local content addressed object folder, "
    "execution local spool and cursor folders and a deterministic hash embedder stand in for concurrent agents, the "
    "central database, the archive bucket and the embedding provider; ack latency and lag run on a fake clock, and "
    "search wall time is measured separately by the wall clock test; no model, HTTP server, vector database or Pod runs"
)
COUNTS = (
    "cursor_commits_lost",
    "acknowledgements_lost",
    "spool_full_refusals",
    "cursor_moved_on_refusal",
    "dropped_batches",
    "searches_answered",
    "restricted_searches_refused",
    "out_of_scope_results",
    "needle_hits",
)


def counts(run: Run) -> dict:
    return {name: run.counts[name] for name in COUNTS}


def loaded(spec: dict, seed: int, root: Path, variant: Variant = Variant()) -> tuple[Run, dict, dict]:
    run = Run(spec, seed, root, variant)
    drained = run.load()
    return run, drained, run.verify()


def report(run: Run, drained: dict, verdict: dict) -> dict:
    return {"seed": run.seed, **drained, "verdict": verdict, "counts": counts(run), **run.metrics()}


def healthy(run: Run, drained: dict, verdict: dict) -> None:
    searched = run.series[-1]["tick"] // run.spec["search_every"]
    assert verdict["complete"], verdict["failures"]
    assert drained["drained_at_tick"] is not None
    assert verdict["authoritative_events"] == verdict["transcript_lines"]
    assert run.counts["out_of_scope_results"] == 0 and run.counts["restricted_searches_refused"] == searched
    assert run.invisible_markers() == 0 and run.counts["cursor_moved_on_refusal"] == 0


def positive(spec: dict, root: Path) -> dict:
    seeds = {}
    for seed in spec["seeds"]:
        run, drained, verdict = loaded(spec, seed, root / f"seed-{seed}")
        healthy(run, drained, verdict)
        assert run.counts["cursor_commits_lost"] and run.counts["acknowledgements_lost"]
        assert verdict["duplicate_events_total"] > 0 and verdict["re_embedded_texts"] == 0
        assert run.metrics()["peak"]["spool_pending_batches"] > 0
        seeds[str(seed)] = {**report(run, drained, verdict), "series": run.series}
    hashes = {item["verdict"]["source_sha256"] for item in seeds.values()}
    assert len(hashes) == len(seeds)
    return {"runs": seeds, "independent_fixtures_differ": True}


def scope_refusals(run: Run) -> dict:
    projects = run.spec["projects"]
    narrow, wide = run.grants()
    before = run.central.protected()
    attempts = {
        "restricted_project": run.ask(wide, load.NEEDLE, [projects["restricted"]]),
        "restricted_with_granted": run.ask(wide, load.NEEDLE, [projects["granted"], projects["restricted"]]),
        "project_outside_a_narrow_grant": run.ask(narrow, load.NEEDLE, [projects["other"]]),
        "foreign_corpus": run.ask(narrow, load.NEEDLE, domain="personal-corpus"),
    }
    untouched = run.central.protected() == before
    diagnostics = json.dumps(attempts, ensure_ascii=False)
    hidden = [projects["restricted"], projects["other"], "writer-", "background-", "session:"]
    details = {answer["error"]["detail"] for answer in attempts.values()}
    assert untouched and len(details) == 1 and not any(name in diagnostics for name in hidden)
    assert all(sorted(answer) == ["error", "operation_id", "schema_version", "state"] for answer in attempts.values())
    corrected = run.ask(narrow, load.NEEDLE, [projects["granted"]], 50)
    allowed = {projects["granted"]}
    assert corrected["state"] == "answered" and corrected["results"]
    assert all(set(item["project_ids"]) <= allowed for item in corrected["results"])
    return {
        "rejected": {name: answer["error"] for name, answer in attempts.items()},
        "protected_state_unchanged": untouched,
        "hidden_names_absent_from_diagnostics": True,
        "session_search_scope_rejections": run.central.searcher.scope_rejections_total(run.central.domain),
        "corrected": {"state": corrected["state"], "results": len(corrected["results"]), "out_of_scope": 0},
    }


FAILURE_KINDS = (
    ("events for", "events_missing"),
    ("archive", "archive_incomplete"),
    ("spool", "spool_backlog"),
    ("wait for embedding", "embedding_backlog"),
    ("embedded more", "re_embedded"),
    ("vector", "unindexed"),
)


def failure_kinds(failures: list[str]) -> dict:
    kinds = Counter(next(kind for text, kind in FAILURE_KINDS if text in failure) for failure in failures)
    return dict(sorted(kinds.items()))


def rejection(spec: dict, root: Path) -> dict:
    seed, settings = spec["seeds"][0], spec["rejection"]
    variant = Variant(overload_spool_bytes=settings["spool_max_bytes"])
    run, drained, verdict = loaded(spec, seed, root / "spool-pressure", variant)
    healthy(run, drained, verdict)
    assert run.counts["spool_full_refusals"] > 0
    dropping, dropped_drain, dropped = loaded(
        spec, seed, root / "dropping", Variant(drop_older_than=settings["drop_older_than_seconds"])
    )
    assert run.counts["dropped_batches"] == 0 and dropping.counts["dropped_batches"] > 0
    assert not dropped["complete"] and dropped["authoritative_events"] < dropped["transcript_lines"]
    honest = run.metrics()["ingest_ack_latency_seconds"]["written_during_overload"]
    shortcut = dropping.metrics()["ingest_ack_latency_seconds"]["written_during_overload"]
    return {
        "spool_pressure": {
            "spool_max_bytes_while_central_down": settings["spool_max_bytes"],
            **report(run, drained, verdict),
        },
        "dropping_uploader": {
            "drop_older_than_seconds": settings["drop_older_than_seconds"],
            "graded": "failed" if not dropped["complete"] else "passed",
            "failure_kinds": failure_kinds(dropped["failures"]),
            "authoritative_events": dropped["authoritative_events"],
            "transcript_lines": dropped["transcript_lines"],
            "dropped_batches": dropping.counts["dropped_batches"],
            "ack_latency_written_during_overload": shortcut,
            "honest_ack_latency_written_during_overload": honest,
            **dropped_drain,
        },
        "scope": scope_refusals(run),
    }


def replayed(run: Run) -> dict:
    execution = run.writers[1]
    entry = execution.delivered[0]
    before, duplicates = run.central.protected(), run.central.catalog.duplicate_events_total(run.central.domain)
    result = run.central.upload(execution, entry)
    after = run.central.protected() == before
    gained = run.central.catalog.duplicate_events_total(run.central.domain) - duplicates
    first = entry.records[0]
    content = first["raw"]["message"]["content"]
    swapped = ("Z" if content[0] != "Z" else "Y") + content[1:]
    raw = {**first["raw"], "message": {**first["raw"]["message"], "content": swapped}}
    altered = replace(entry, records=[{**first, "text": swapped, "raw": raw}, *entry.records[1:]])
    try:
        run.central.upload(execution, altered)
        refused = None
    except Conflict as error:
        refused = str(error)
    kept = run.central.protected() == before
    assert result["state"] == "replayed" and after and gained == len(entry.records) and refused and kept
    return {
        "same_identity_replay": {"state": result["state"], "authoritative_state_unchanged": after},
        "duplicate_events_counted": gained,
        "older_attempt_with_other_content": {"refused": refused, "authoritative_state_unchanged": kept},
    }


def recovery(spec: dict, root: Path) -> dict:
    variant = Variant(restart_at=spec["overload"]["restart_at"])
    run, drained, verdict = loaded(spec, spec["seeds"][0], root, variant)
    healthy(run, drained, verdict)
    assert run.restarted["spool_backlog_survived"] and run.restarted["batches"] > 0
    stale = run.stale_cursor()
    assert stale == {"refused": "stale_cursor", "cursor_unchanged": True}
    overload = [row for row in run.series if row["central"] == "unavailable"]
    after = [row for row in run.series if row["tick"] >= spec["overload"]["until"]]
    assert all(row["embeddings"] == "paused" for row in overload) and verdict["re_embedded_texts"] == 0
    assert max(row["spool_pending_batches"] for row in overload) > 0 and after[-1]["spool_pending_batches"] == 0
    return {
        **report(run, drained, verdict),
        "restart": {"at_tick": variant.restart_at, **run.restarted},
        "stale_cursor": stale,
        "replay": replayed(run),
        "series": run.series,
        "rollback_rehearsal": {
            "action": "embeddings paused for the whole central outage while capture kept spooling durably",
            "paused_ticks": [row["tick"] for row in overload],
            "peak_spool_pending_batches_while_paused": max(row["spool_pending_batches"] for row in overload),
            "embedding_pending_after_resume": max(row["embedding_pending_chunks"] for row in after),
            "drained_at_tick": drained["drained_at_tick"],
        },
    }


CASES = {"a": positive, "b": rejection, "c": recovery}


def run_case(case: str) -> dict:
    spec = load.load_spec()
    with tempfile.TemporaryDirectory() as folder:
        observed = CASES[case](spec, Path(folder))
    return {
        "case": f"T-SV2-PRF-03-{case.upper()}",
        "state": "passed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {load.WORKLOAD.name: hashlib.sha256(load.WORKLOAD.read_bytes()).hexdigest()},
        "observed": observed,
    }
