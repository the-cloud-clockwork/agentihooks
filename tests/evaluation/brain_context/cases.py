import hashlib
import json
import tempfile
from pathlib import Path

from tests.evaluation.brain_context import evaluate
from tests.evaluation.brain_context.evaluate import Refused, Registry

EVIDENCE_CLASS = (
    "mocked: a fixed held out corpus with human review labels and in process packers that follow the kernel ranking "
    "contract stand in for brain-api, its graph store and a live model; no embedding, HTTP server or agent runs, and "
    "usefulness is the share of injected items a reviewer labelled useful, not a model judgement"
)
FIXTURES = ("alpha", "beta")
COMPARED = ("feed_only", "scoped", "graph")
REJECTED = ("graph_wide", "scoped_padded", "feed_only")
HEADLINE = (
    "context_source_recall",
    "reviewed_usefulness",
    "repeated_mistakes_avoided",
    "scope_leakage",
    "irrelevant_injection",
    "stale_injection",
    "arcs",
    "context_tokens",
    "context_tokens_per_found_source",
)


def fixture_sha256(corpus: dict, name: str) -> str:
    return hashlib.sha256(json.dumps(corpus["fixtures"][name], sort_keys=True).encode()).hexdigest()


def headline(report: dict) -> dict:
    return {key: report["totals"][key] for key in HEADLINE}


def per_case(reports: dict[str, dict]) -> dict:
    tasks = next(iter(reports.values()))["tasks"]
    return {
        task: {
            "case": tasks[task]["case"],
            **{
                name: {"found": r["tasks"][task]["found"], "injected": r["tasks"][task]["injected"]}
                for name, r in reports.items()
            },
        }
        for task in tasks
    }


def request(operation_id: str, kind: str, report: dict, sequence: int) -> dict:
    return {"operation_id": operation_id, "kind": kind, "expected_sequence": sequence, "report": report}


def seeded(corpus: dict, name: str, root: Path) -> tuple[Registry, dict]:
    accepted = evaluate.evaluate(corpus["accepted_policy"], name, corpus)
    registry = Registry(root / f"{name}-accepted.json")
    registry.seed(accepted)
    return registry, accepted


def improves(scoped: dict, graph: dict) -> None:
    before, after = scoped["totals"], graph["totals"]
    assert after["context_source_recall"] > before["context_source_recall"]
    assert after["scope_leakage"] == 0 and after["irrelevant_injection"] <= before["irrelevant_injection"]
    assert after["stale_injection"] == 0 and after["reviewed_usefulness"] >= before["reviewed_usefulness"]
    lesson = [task for task, row in graph["tasks"].items() if row["case"] == "held_out_incident"]
    assert all(graph["tasks"][task]["found"] == graph["tasks"][task]["expected"] for task in lesson)
    assert all(scoped["tasks"][task]["found"] < scoped["tasks"][task]["expected"] for task in lesson)


def promoted(corpus: dict, name: str, root: Path) -> dict:
    reports = {policy: evaluate.evaluate(policy, name, corpus) for policy in COMPARED}
    improves(reports["scoped"], reports["graph"])
    assert reports["feed_only"]["totals"]["scope_leakage"] > 0
    registry, accepted = seeded(corpus, name, root)
    result = registry.submit(request(f"{name}-promote-graph", "promote", reports["graph"], 1))
    record = registry.read()
    assert result == {"state": "accepted", "sequence": 2} and record["policy"] == "graph"
    assert record["verdict"] == evaluate.qualify(accepted["totals"], reports["graph"]["totals"])
    assert record["report_sha256"] == evaluate.digest(evaluate.evaluate("graph", name, corpus))
    return {
        "fixture_sha256": fixture_sha256(corpus, name),
        "actor": accepted["actor"],
        "totals": {policy: headline(report) for policy, report in reports.items()},
        "per_case": per_case(reports),
        "verdict": record["verdict"],
        "accepted_record": {key: record[key] for key in ("sequence", "policy", "report_sha256")},
    }


def positive(corpus: dict, root: Path) -> dict:
    fixtures = {name: promoted(corpus, name, root) for name in FIXTURES}
    assert len({item["fixture_sha256"] for item in fixtures.values()}) == len(FIXTURES)
    return {"fixtures": fixtures, "independent_fixtures_differ": True}


def refused(registry: Registry, payload: dict) -> str:
    before = registry.path.read_bytes()
    try:
        registry.submit(payload)
    except Refused as error:
        assert registry.path.read_bytes() == before and not registry.staged.exists()
        return str(error)
    raise AssertionError(f"{payload['operation_id']} was accepted")


def foreign_names(corpus: dict, name: str) -> list[str]:
    fixture = corpus["fixtures"][name]
    granted = set(fixture["grant"]["projects"])
    outside = [item for item in fixture["items"] if item["project"] not in granted]
    return sorted(
        {item["id"] for item in outside} | {item["project"] for item in outside} | {i["title"] for i in outside}
    )


def contained(corpus: dict, name: str, root: Path) -> dict:
    registry, accepted = seeded(corpus, name, root)
    attempts = {}
    for index, policy in enumerate(REJECTED):
        report = evaluate.evaluate(policy, name, corpus)
        verdict = evaluate.qualify(accepted["totals"], report["totals"])
        assert not verdict["qualified"] and verdict["regressions"], verdict
        asserted = {**request(f"{name}-reject-{index}", "promote", report, 1), "verdict": {"qualified": True}}
        attempts[policy] = {"verdict": verdict, "refused": refused(registry, asserted), "totals": headline(report)}
    wide = attempts["graph_wide"]["verdict"]
    assert {"irrelevant_injection", "stale_injection"} <= set(wide["regressions"]) and wide["volume_delta"]["arcs"] > 0
    padded = attempts["scoped_padded"]["verdict"]
    assert "irrelevant_injection" in padded["regressions"] and padded["volume_delta"]["context_tokens"] > 0
    assert "scope_leakage" in attempts["feed_only"]["verdict"]["regressions"]
    graph = evaluate.evaluate("graph", name, corpus)
    louder = {
        **graph["totals"],
        "arcs": graph["totals"]["arcs"] + 3,
        "context_tokens": graph["totals"]["context_tokens"] + 400,
    }
    volume_only = evaluate.qualify(graph["totals"], louder)
    assert volume_only["reason"] == "no_gain" and not volume_only["qualified"]
    result = registry.submit(request(f"{name}-promote-graph", "promote", graph, 1))
    assert result == {"state": "accepted", "sequence": 2}
    observed = {
        "attempts": attempts,
        "caller_verdict_ignored": True,
        "volume_only": volume_only,
        "protected_state_unchanged_by_refusals": True,
        "corrected": {"request": "new operation id against sequence 1", **result},
    }
    assert not any(text in json.dumps(observed) for text in foreign_names(corpus, name))
    return {**observed, "foreign_names_absent_from_diagnostics": True}


def rejection(corpus: dict, root: Path) -> dict:
    return {name: contained(corpus, name, root) for name in FIXTURES}


def reverted(corpus: dict, root: Path) -> tuple[dict, Registry, dict]:
    name = FIXTURES[0]
    registry, _ = seeded(corpus, name, root)
    registry.submit(request("promote-graph", "promote", evaluate.evaluate("graph", name, corpus), 1))
    rerun = evaluate.evaluate(corpus["accepted_policy"], name, corpus)
    revert = request("revert-to-scoped", "revert", rerun, 2)
    registry.stage(registry.plan(revert)["record"])
    survived = registry.read()
    restarted = Registry(registry.path)
    recovered = restarted.recover()
    assert recovered == {"discarded_staged_write": True, "sequence": 2} and survived["policy"] == "graph"
    applied = restarted.submit(revert)
    replay = restarted.submit(revert)
    record = restarted.read()
    assert applied == {"state": "accepted", "sequence": 3} and replay == {"state": "replayed", "sequence": 3}
    assert len(record["history"]) == 3 and record["report_sha256"] == record["history"][0]["report_sha256"]
    observed = {
        "interrupted_revert": {
            "interruption": "simulated: the revert was staged and the registry reopened before the rename",
            "survived": {key: survived[key] for key in ("sequence", "policy")},
            **recovered,
        },
        "applied": applied,
        "same_operation_replay": replay,
        "reproducible": {
            "seed_report_sha256": record["history"][0]["report_sha256"],
            "revert_report_sha256": record["report_sha256"],
            "equal": True,
        },
    }
    return observed, restarted, rerun


def recovery(corpus: dict, root: Path) -> dict:
    observed, registry, rerun = reverted(corpus, root)
    graph = evaluate.evaluate("graph", FIXTURES[0], corpus)
    drifted = {**rerun, "totals": {**rerun["totals"], "arcs": rerun["totals"]["arcs"] + 1}}
    outcomes = {
        "stale_sequence": refused(registry, request("late-promote-graph", "promote", graph, 2)),
        "other_corpus_revision": refused(
            registry, request("other-revision", "promote", {**graph, "corpus_revision": "sv2-prf-04-corpus-0"}, 3)
        ),
        "operation_id_reused": refused(registry, request("revert-to-scoped", "revert", graph, 3)),
        "irreproducible_report": refused(registry, request("revert-drifted", "revert", drifted, 3)),
    }
    assert outcomes == {key: key for key in outcomes}
    record = registry.read()
    assert record["sequence"] == 3 and record["policy"] == corpus["accepted_policy"]
    return {
        **observed,
        "refused": outcomes,
        "final": {key: record[key] for key in ("sequence", "policy", "report_sha256")},
        "history": [
            {key: entry[key] for key in ("operation_id", "kind", "sequence", "policy")} for entry in record["history"]
        ],
        "rollback_rehearsal": {
            "action": "restore the last accepted ranking and packing policy",
            "restored": record["policy"],
            "from": record["history"][-2]["policy"],
            "comparison_reproduced_on_corpus_revision": record["corpus_revision"],
        },
    }


CASES = {"a": positive, "b": rejection, "c": recovery}
INPUTS = (evaluate.CORPUS, Path(evaluate.__file__), Path(__file__))


def run_case(case: str) -> dict:
    corpus = evaluate.load_corpus()
    with tempfile.TemporaryDirectory() as folder:
        observed = CASES[case](corpus, Path(folder))
    return {
        "case": f"T-SV2-PRF-04-{case.upper()}",
        "state": "passed",
        "evidence_class": EVIDENCE_CLASS,
        "corpus_revision": corpus["corpus_revision"],
        "input_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in INPUTS},
        "observed": observed,
    }
