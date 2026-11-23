from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.ci_lab import checks, delivery, shadow
from scripts.doctor import cli as doctor
from scripts.doctor import detect, lab
from scripts.swarm import cli as swarm_cli
from scripts.swarm import lab_capacity
from scripts.swarm.health.findings import Finding
from scripts.swarm.store import RedisStore

MINUTE = 60_000
NOW = 1_794_960_000_000
SLUG = "sw"
URL = "http://lab.example:8080"
REPOSITORY = "owner/repo"
APP = "agentihooks-lab"
HEAD = "a" * 40
OTHER = "b" * 40
ROOT = Path(__file__).resolve().parents[2]
LIMITS = lab.Limits(20, 0.6)


def entry(wait_s: float | None = 60.0, ratio: float | None = 0.9) -> dict[str, Any]:
    return {"pending": 3, "running": 2, "predicted_wait_s": wait_s, "verdict_cache_hit_ratio": ratio}


def block(classes: dict[str, Any], at: int = NOW - MINUTE, error: str = "", **extra: Any) -> dict[str, Any]:
    capacity = {
        "classes": classes,
        "slots": {"total": 8, "busy": 5, "free": 3},
        "utilisation": 0.62,
        "budget_fraction": 0.3,
        "shed": [],
    }
    return {"url": URL, "at": at, "capacity": capacity, "error": error, "error_at": NOW if error else 0, **extra}


def faked() -> dict[str, Any]:
    return block(
        {"pull_request": entry(1500.0, 0.45), "queue": entry(None, 0.6), "promotion": entry(30.0, None)},
    )


def behind_finding(minutes: int = 25) -> Finding:
    return Finding(
        "lab behind",
        SLUG,
        f"the lab is {minutes} minutes behind on pull request grading",
        (f"pull request grading predicted wait {minutes} minutes", f"capacity read at {NOW - MINUTE}"),
        "a predicted pull request grading wait above 20 minutes",
        minutes,
    )


def ratio_finding() -> Finding:
    return Finding(
        "lab cache hit ratio",
        "pull_request",
        "the lab verdict cache answered 45 percent of pull_request lookups, under the floor of 60 percent",
        ("class pull_request verdict cache hit ratio 0.45", f"capacity read at {NOW - MINUTE}"),
        "a verdict cache hit ratio below 0.6 (AGENTIHOOKS_DOCTOR_LAB_HIT_FLOOR)",
        45,
    )


@pytest.mark.feature("doctor-lab-health")
def test_an_unreachable_lab_is_one_finding_measured_from_its_last_good_read() -> None:
    stale = block({"pull_request": entry(3000.0, 0.1)}, at=NOW - 7 * MINUTE, error="timed out")
    assert lab.findings(SLUG, stale, NOW, LIMITS) == [
        Finding(
            "lab unreachable",
            SLUG,
            f"the lab at {URL} has not answered a capacity read for 7 minutes: timed out",
            (f"lab {URL}", f"error at {NOW}: timed out", f"last good read at {NOW - 7 * MINUTE}"),
            "the last capacity read failed",
            7,
        )
    ]
    never = {"url": URL, "error": "connection refused", "error_at": NOW - 2 * MINUTE}
    [found] = lab.findings(SLUG, never, NOW, LIMITS)
    assert found.id == "lab-unreachable/sw"
    assert found.measure == 2
    assert found.evidence[2] == "last good read at never"
    assert lab.findings(SLUG, None, NOW, LIMITS) == []


@pytest.mark.feature("doctor-lab-health")
def test_a_fresh_reading_behind_the_limit_is_a_finding_and_at_the_limit_is_not() -> None:
    assert lab.findings(SLUG, block({"pull_request": entry(1500.0)}), NOW, LIMITS) == [behind_finding()]
    assert lab.findings(SLUG, block({"pull_request": entry(1200.0)}), NOW, LIMITS) == []
    assert lab.findings(SLUG, block({"pull_request": entry(1500.0)}, at=NOW - 11 * MINUTE), NOW, LIMITS) == []
    assert lab.findings(SLUG, block({"queue": entry(9000.0)}), NOW, LIMITS) == []
    assert lab.findings(SLUG, block({"pull_request": entry(1500.0)}), NOW, lab.Limits(25, 0.6)) == []


@pytest.mark.feature("doctor-lab-health")
def test_each_class_with_a_hit_ratio_under_the_floor_is_a_finding() -> None:
    assert lab.findings(SLUG, faked(), NOW, LIMITS) == [behind_finding(), ratio_finding()]
    assert lab.findings(SLUG, faked(), NOW, lab.Limits(30, 0.4)) == []
    late = block({"pull_request": entry(60.0, 0.1)}, at=NOW - 11 * MINUTE)
    assert lab.findings(SLUG, late, NOW, LIMITS) == []
    edge = block({"queue": entry(None, 0.1)}, at=NOW - 10 * MINUTE)
    assert [found.subject for found in lab.findings(SLUG, edge, NOW, LIMITS)] == ["queue"]
    [rounded] = lab.findings(SLUG, block({"queue": entry(None, 0.55)}), NOW, LIMITS)
    assert rounded.measure == 55
    assert "answered 55 percent of queue lookups" in rounded.summary
    assert lab.findings(SLUG, block({"pull_request": {"predicted_wait_s": None}}), NOW, LIMITS) == []


@pytest.mark.feature("doctor-lab-health")
def test_the_wait_limit_and_hit_floor_come_from_the_environment() -> None:
    assert lab.Limits.from_env({}) == lab.Limits(20, 0.6)
    environ = {"AGENTIHOOKS_HEALTH_CI_BEHIND_MINUTES": "30", "AGENTIHOOKS_DOCTOR_LAB_HIT_FLOOR": "0.8"}
    assert lab.Limits.from_env(environ) == lab.Limits(30, 0.8)
    assert lab.Limits.from_env({"AGENTIHOOKS_DOCTOR_LAB_HIT_FLOOR": "1"}).hit_floor == 1.0
    assert lab.Limits.from_env({"AGENTIHOOKS_DOCTOR_LAB_HIT_FLOOR": ".5"}).hit_floor == 0.5
    for malformed in ("", "x", "1.5", "-0.2", "nan", "0.5 "):
        assert lab.Limits.from_env({"AGENTIHOOKS_DOCTOR_LAB_HIT_FLOOR": malformed}).hit_floor == 0.6


@pytest.mark.feature("doctor-lab-health")
def test_an_authoritative_gate_with_a_mismatch_is_a_finding() -> None:
    assert lab.gate_findings({"lint": 2, "docs": 0}, 10) == [
        Finding(
            "lab gate mismatch",
            "lint",
            "the authoritative gate lint disagrees with GitHub Actions on 2 of the newest 10 pull request heads",
            (
                "gate lint is authoritative in delivery.yaml",
                "lab check lab/lint against the github-actions job on the newest 10 pull request heads",
            ),
            "any mismatch on an authoritative gate",
            2,
        )
    ]
    assert lab.gate_findings({}, 10) == []


def check_run(name: str, conclusion: str, minute: int, slug: str) -> dict[str, Any]:
    return {
        "id": minute,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "completed_at": f"2026-11-18T00:{minute:02d}:00Z",
        "app": {"slug": slug},
    }


def answers(gate: str) -> dict[str, dict[str, Any]]:
    runs = f"repos/{REPOSITORY}/actions/workflows/test.yml/runs?event=pull_request&status=completed"
    checks = "check-runs?filter=latest&per_page=100&page=1"
    lab_run = f"lab-shadow/{gate}"
    return {
        f"{runs}&per_page=100&page=1": {"workflow_runs": [{"head_sha": HEAD}, {"head_sha": OTHER}]},
        f"repos/{REPOSITORY}/commits/{HEAD}/{checks}": {
            "check_runs": [
                check_run(gate, "failure", 10, "github-actions"),
                check_run(lab_run, "success", 5, "agentihooks-lab"),
            ]
        },
        f"repos/{REPOSITORY}/commits/{OTHER}/{checks}": {
            "check_runs": [
                check_run(gate, "success", 10, "github-actions"),
                check_run(lab_run, "success", 5, "agentihooks-lab"),
            ]
        },
    }


@pytest.mark.feature("doctor-lab-health")
def test_mismatches_count_a_listed_gate_over_the_newest_heads_reading_each_head_once() -> None:
    asked: list[str] = []
    found = answers("lint")

    def fetch(path: str) -> dict[str, Any]:
        asked.append(path)
        return found[path]

    sources = shadow.Sources(frozenset({"lint"}), APP)
    assert lab.mismatches(fetch, REPOSITORY, ("lint",), sources, 2) == {"lint": 1}
    assert asked == list(found)
    asked.clear()
    assert lab.mismatches(fetch, REPOSITORY, ("lint",), sources, 1) == {"lint": 1}
    assert asked == list(found)[:2]
    assert lab.mismatches(fetch, REPOSITORY, ("lint",), shadow.Sources(frozenset({"lint"}), "other"), 2) == {"lint": 0}
    assert lab.mismatches(fetch, REPOSITORY, (), sources, 2) == {}


@pytest.mark.feature("doctor-lab-health")
def test_the_gate_reader_asks_github_only_for_authoritative_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    config = SimpleNamespace(repo=str(ROOT), repo_origin=f"github.com/{REPOSITORY}")
    store = SimpleNamespace(config=lambda slug: config if slug == SLUG else None)

    def refused(path: str) -> dict[str, Any]:
        raise AssertionError(path)

    named = {"AGENTIHOOKS_LAB_APP": APP}
    monkeypatch.setattr(delivery, "authoritative", lambda root: ())
    assert lab.read_gates(store, SLUG, {}, refused) == []
    seen: list[Path] = []
    monkeypatch.setattr(delivery, "authoritative", lambda root: seen.append(root) or ("lint",))
    with pytest.raises(ValueError, match="^AGENTIHOOKS_LAB_APP names no lab App, so authoritative gate mismatches"):
        lab.read_gates(store, SLUG, {}, refused)
    monkeypatch.setattr(shadow, "static_jobs", lambda root: seen.append(root) or frozenset({"lint"}))
    found = answers("lint")
    [mismatch] = lab.read_gates(store, SLUG, named, lambda path: found[path])
    assert mismatch == lab.gate_findings({"lint": 1}, 10)[0]
    assert seen == [ROOT, ROOT, ROOT]
    monkeypatch.setattr(lab, "HEADS", 1)
    asked: list[str] = []
    [windowed] = lab.read_gates(store, SLUG, named, lambda path: asked.append(path) or found[path])
    assert windowed == lab.gate_findings({"lint": 1}, 1)[0]
    assert asked == list(found)[:2]


GATES = ("lint", "type-gate", "test-count")
RUNS = f"repos/{REPOSITORY}/actions/workflows/test.yml/runs?event=pull_request&status=completed&per_page=100&page=1"


def checks_path(sha: str, page: int = 1) -> str:
    return f"repos/{REPOSITORY}/commits/{sha}/check-runs?filter=latest&per_page=100&page={page}"


@pytest.mark.feature("doctor-lab-health")
def test_a_pass_reads_the_newest_ten_heads_once_for_every_listed_gate() -> None:
    heads = [f"{index:040x}" for index in range(12)]
    skipped = [check_run(gate, "skipped", 10, "github-actions") for gate in GATES]
    posted = [check_run(checks.name(gate, shadow=False), "success", 5, APP) for gate in GATES]
    filler = [check_run(f"unit (3.12, {index})", "success", 10, "github-actions") for index in range(97)]
    found: dict[str, dict[str, Any]] = {
        RUNS: {"workflow_runs": [{"head_sha": sha} for sha in [heads[0], *heads]]},
        **{checks_path(sha): {"check_runs": [*skipped, *posted]} for sha in heads},
        checks_path(heads[0]): {"check_runs": [*skipped, *filler]},
        checks_path(heads[0], 2): {"check_runs": posted},
    }
    asked: list[str] = []

    def fetch(path: str) -> dict[str, Any]:
        asked.append(path)
        return found[path]

    sources = shadow.Sources(frozenset(GATES), APP)
    assert lab.mismatches(fetch, REPOSITORY, GATES, sources, 10) == dict.fromkeys(GATES, 0)
    assert asked == [RUNS, checks_path(heads[0]), checks_path(heads[0], 2), *map(checks_path, heads[1:10])]
    asked.clear()
    found[checks_path(heads[3])] = {"check_runs": [check_run("type-gate", "failure", 10, "github-actions"), *posted]}
    assert lab.mismatches(fetch, REPOSITORY, GATES, sources, 10) == {"lint": 0, "type-gate": 1, "test-count": 0}
    assert len(asked) == 12
    asked.clear()
    assert lab.mismatches(fetch, REPOSITORY, (), sources, 10) == {}
    assert asked == []


@pytest.mark.feature("doctor-lab-health")
def test_a_listed_gate_is_compared_under_the_name_the_lab_posts() -> None:
    def counted(*runs: dict[str, Any], app: str = APP) -> dict[str, int]:
        found = {RUNS: {"workflow_runs": [{"head_sha": HEAD}]}, checks_path(HEAD): {"check_runs": list(runs)}}
        return lab.mismatches(found.__getitem__, REPOSITORY, ("lint",), shadow.Sources(frozenset({"lint"}), app), 10)

    assert (checks.name("lint", shadow=False), checks.name("lint", shadow=True)) == ("lab/lint", "lab-shadow/lint")
    passed = check_run("lint", "success", 10, "github-actions")
    refused = check_run("lab/lint", "failure", 5, APP)
    assert counted(passed, refused) == {"lint": 1}
    assert counted(passed, refused, app="other") == {"lint": 0}
    assert counted(check_run("lint", "skipped", 10, "github-actions"), refused) == {"lint": 0}
    assert counted(passed, check_run("lab/type-gate", "failure", 5, APP)) == {"lint": 0}
    assert counted(passed, check_run("lab-shadow/lint", "success", 4, APP), refused) == {"lint": 1}
    assert counted(passed, refused, check_run("lab-shadow/lint", "success", 6, APP)) == {"lint": 0}


@pytest.fixture
def store() -> RedisStore:
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


@pytest.mark.feature("doctor-lab-health")
def test_the_doctor_reads_the_stored_lab_block_and_registers_both_readers(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.redis.set(store.key(SLUG, lab_capacity.KEY), json.dumps(faked()))
    assert lab.read(store, SLUG, NOW, {}) == [behind_finding(), ratio_finding()]
    assert lab.read(store, SLUG, NOW, {"AGENTIHOOKS_DOCTOR_LAB_HIT_FLOOR": "0.4"}) == [behind_finding()]
    readers = detect.readers(store, None, SLUG, NOW, environ={"AGENTIHOOKS_HEALTH_CI_BEHIND_MINUTES": "30"})
    assert readers["lab"]() == [ratio_finding()]
    monkeypatch.setattr(lab, "read_gates", lambda given, slug, environ: [given, slug, environ])
    assert readers["lab gate"]() == [store, SLUG, {"AGENTIHOOKS_HEALTH_CI_BEHIND_MINUTES": "30"}]


@pytest.mark.feature("doctor-lab-health")
def test_doctor_findings_prints_a_faked_lab_reading_as_json(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store.redis.set(store.key(SLUG, lab_capacity.KEY), json.dumps(faked()))
    monkeypatch.setattr(swarm_cli, "connect", lambda: store)
    monkeypatch.setattr(swarm_cli, "now_ms", lambda: NOW)
    monkeypatch.setattr(swarm_cli, "LedgerClient", lambda: None)
    assert doctor.main([SLUG, "findings", "--detector", "lab", "--json"]) == 0
    expected = [{**f.as_dict(), "id": f.id, "measure": f.measure} for f in (behind_finding(), ratio_finding())]
    assert json.loads(capsys.readouterr().out) == json.loads(json.dumps(expected))
    assert doctor.main([SLUG, "findings", "--detector", "lab"]) == 0
    assert capsys.readouterr().out.splitlines() == [f"{f.id} {f.summary}" for f in (behind_finding(), ratio_finding())]
    assert doctor.main([SLUG, "findings", "--detector", "lab", "--detector", "nothing", "--detector", "none"]) == 1
    err = capsys.readouterr().err
    assert "doctor: no detector none, nothing; the detectors are " in err
    assert ", lab, lab gate, " in err and "trace" not in err
    watched: list[Any] = []
    calls: list[tuple[Any, ...]] = []
    fakes = {"lab": lambda: [behind_finding()], "more": lambda: [ratio_finding()]}
    monkeypatch.setattr(doctor, "_watched", lambda given, slug: watched.append(given) or slug)
    monkeypatch.setattr(swarm_cli, "LedgerClient", lambda: "ledger")
    monkeypatch.setattr(detect, "readers", lambda *args, **kwargs: calls.append((*args, kwargs)) or fakes)
    assert doctor.main([SLUG, "findings"]) == 0
    assert capsys.readouterr().out.splitlines() == [f"{f.id} {f.summary}" for f in (behind_finding(), ratio_finding())]
    assert watched == [store]
    assert calls == [(store, "ledger", SLUG, NOW, {"telemetry": False})]
    [commands] = [a for a in doctor.build_parser()._actions if isinstance(a, argparse._SubParsersAction)]
    helps = {choice.dest: choice.help for choice in commands._choices_actions}
    assert helps["findings"] == "run the detectors once and print their findings"
