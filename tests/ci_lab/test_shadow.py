from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

import scripts.ci_lab.__main__ as cli
from scripts.ci_lab import checks, contract, metrics, shadow
from scripts.ci_lab.objects import LocalObjects
from scripts.swarm import metrics_outbox

if TYPE_CHECKING:
    import fakeredis

ROOT = Path(__file__).resolve().parents[2]
HEAD = "a" * 40
OTHER = "b" * 40
URL = "redis://lab.example:6379/0"
NOW_MS = 1_700_000_000_000
LAB_APP = "agentihooks-lab"
STATIC = frozenset({"lint", "type-gate", "semgrep/scan", "semgrep/charts", "semgrep/feature-tests"})
CHECK_RUNS = {
    "promote": "unmapped",
    "Promotion ready": "unmapped",
    "stage-budget": "unmapped",
    "Gate — Required": "unmapped",
    "test-summary": "unmapped",
    "refresh-durations": "unmapped",
    "github.event_name == 'merge_group' && 'Gate — Required' || 'queue-fail-fast'": "unmapped",
    "Launch hotfix grade": "unmapped",
    "shard-check": "unmapped",
    "coverage-baseline": "unmapped",
    "unit rerun (${{ matrix.python-version }}, ${{ matrix.shard }})": "tests",
    "unit-lost": "tests",
    "coverage-ratchet": "unmapped",
    "dep-manifest": "unmapped",
    "flake-budget": "unmapped",
    "unit (${{ matrix.python-version }}, ${{ matrix.shard }})": "tests",
    "sonar": "unmapped",
    "split": "unmapped",
    "unit-selected (3.12, 3)": "tests",
    "unit-selected (3.12, 1)": "tests",
    "unit-selected (3.12, 2)": "tests",
    "writer-lock / features": "unmapped",
    "writer-lock / events": "unmapped",
    "writer-lock / tick": "unmapped",
    "writer-lock / scenario": "unmapped",
    "writer-lock / hive-join": "unmapped",
    "mutation": "mutation",
    "mutation-replan": "mutation",
    "mutation-shard": "mutation",
    "mutation-probe": "mutation",
    "mutation-refine": "mutation",
    "mutation-stats": "mutation",
    "semgrep / charts": "semgrep",
    "semgrep / feature-tests": "semgrep",
    "writer-lock / inputs": "unmapped",
    "semgrep / scan": "semgrep",
    "type-gate (3.12)": "type-gate",
    "docs": "unmapped",
    "test-count (3.12)": "test-count",
    "lint": "lint",
    "feature-suite": "unmapped",
    "tick-pattern-tree": "unmapped",
    "hierarchy-cpu": "unmapped",
    "helm-kind": "unmapped",
    "worker-image": "unmapped",
    "mutation-stats-seed": "mutation",
    "install-smoke": "unmapped",
    "ledger-load": "unmapped",
    "brain-smoke": "unmapped",
    "tick-scale": "unmapped",
    "admission": "unmapped",
    "kind-due": "unmapped",
    "swarm-image": "unmapped",
    "reuse": "unmapped",
    "select": "unmapped",
    "mutation-plan": "mutation",
    "lab-verdict": "unmapped",
    "Gate — Protected": "unmapped",
}


class Spool:
    """A metrics sink that checks every row against its table and keeps it."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, object]]] = []

    def append(self, table: metrics_outbox.Table, rows: list[dict[str, object]]) -> None:
        for row in rows:
            table.check(row)
            self.rows.append((table.name, row))


@pytest.fixture
def client() -> fakeredis.FakeRedis:
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


@pytest.fixture
def blobs(tmp_path: Path) -> LocalObjects:
    return LocalObjects(tmp_path / "objects")


@pytest.fixture
def spool() -> Spool:
    return Spool()


@pytest.fixture
def registry() -> metrics.Registry:
    return metrics.Registry()


@pytest.fixture
def recorder(
    client: fakeredis.FakeRedis, blobs: LocalObjects, spool: Spool, registry: metrics.Registry
) -> shadow.Shadow:
    return shadow.Shadow(client, blobs, spool, registry, shadow.Sources(STATIC, LAB_APP), lambda: NOW_MS)


def delivery(
    name: str,
    conclusion: str | None = "success",
    *,
    head: str = HEAD,
    slug: str = "github-actions",
    action: str = "completed",
    summary: str | None = None,
) -> dict[str, Any]:
    run = {
        "name": name,
        "head_sha": head,
        "conclusion": conclusion,
        "app": {"slug": slug},
        "output": {"summary": summary},
    }
    return {"action": action, "check_run": run}


def lab(
    gate: str, conclusion: str = "success", *, head: str = HEAD, grader: str = "g2", action: str = "completed"
) -> dict[str, Any]:
    output = checks.Output("Shadow grade", "Shadow grade of the head", (), "run-1", grader, "https://lab.example/run-1")
    summary = checks.render(output)["summary"]
    return delivery(f"lab-shadow/{gate}", conclusion, head=head, slug=LAB_APP, action=action, summary=summary)


def event(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def table_row(
    head: str, gate: str, grader: str, lab_verdict: str, actions_verdict: str
) -> tuple[str, dict[str, object]]:
    row: dict[str, object] = {
        "event_id": event(gate, head, lab_verdict, actions_verdict),
        "ledger": "ci-lab",
        "ts_ms": NOW_MS,
        "plan": "",
        "phase": "",
        "slice": "",
        "task": "",
        "head": head,
        "gate": gate,
        "grader": grader,
        "lab_verdict": lab_verdict,
        "actions_verdict": actions_verdict,
        "mismatch": int(lab_verdict != actions_verdict),
    }
    return "lab_shadow", row


def document(head: str, gate: str, grader: str, lab_verdict: str, actions_verdict: str) -> bytes:
    row = {
        "head": head,
        "gate": gate,
        "grader": grader,
        "lab_verdict": lab_verdict,
        "actions_verdict": actions_verdict,
        "mismatch": lab_verdict != actions_verdict,
    }
    return json.dumps(row, sort_keys=True).encode()


def git(repo: Path, *argv: str) -> str:
    identity = ("-c", "user.name=lab", "-c", "user.email=lab@example.invalid")
    done = subprocess.run(["git", "-C", str(repo), *identity, *argv], capture_output=True, text=True, check=True)
    return done.stdout.strip()


def history(tmp_path: Path) -> tuple[Path, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--quiet")
    git(repo, "commit", "--quiet", "--allow-empty", "-m", "first")
    first = git(repo, "rev-parse", "HEAD")
    git(repo, "commit", "--quiet", "--allow-empty", "-m", "second")
    return repo, first, git(repo, "rev-parse", "HEAD")


@pytest.mark.feature("lab-shadow-comparison")
def test_every_actions_check_run_of_a_pull_request_head_maps_to_a_gate_or_is_listed_unmapped(
    client: fakeredis.FakeRedis, blobs: LocalObjects, spool: Spool, registry: metrics.Registry
) -> None:
    static = shadow.static_jobs(ROOT)
    assert {name: shadow.gate_of(name, static) for name in CHECK_RUNS} == CHECK_RUNS
    assert set(CHECK_RUNS.values()) - {shadow.UNMAPPED} <= {shadow.TESTS, shadow.MUTATION, *static}
    recorder = shadow.Shadow(client, blobs, spool, registry, shadow.Sources(static, LAB_APP), lambda: NOW_MS)
    for name in CHECK_RUNS:
        assert recorder.observe(delivery(name, "skipped")) is None
    unmapped = {name for name, gate in CHECK_RUNS.items() if gate == "unmapped"}
    assert client.smembers("agentihooks-ci:shadow-unmapped") == unmapped
    assert client.keys("agentihooks-ci:shadow:*") == []


def test_a_check_run_name_maps_by_prefix_or_by_the_static_job_its_steps_carry() -> None:
    assert shadow.gate_of("type-gate (3.12)", STATIC) == "type-gate"
    assert shadow.gate_of("semgrep / scan", STATIC) == "semgrep/scan"
    assert shadow.gate_of("lint/lint-check", STATIC) == "lint"
    assert shadow.gate_of("semgrep/scan/fail-on-findings-new-against-the-base", STATIC) == "semgrep/scan"
    assert shadow.gate_of("tests", STATIC) == "tests"
    assert shadow.gate_of("unit", frozenset()) == "tests"
    assert shadow.gate_of("mutation", frozenset()) == "mutation"
    assert shadow.gate_of("lintx", STATIC) == "unmapped"
    assert shadow.gate_of("semgrep", STATIC) == "unmapped"
    assert shadow.gate_of("a test", STATIC) == "unmapped"


def test_the_recorder_reads_the_shadow_check_run_names_the_lab_posts() -> None:
    assert shadow.LAB_NAME == checks.name("", shadow=True)


def test_the_recorder_imports_without_the_http_client_the_dashboard_column_checker_lacks() -> None:
    probe = "import sys; sys.modules['requests'] = None; import scripts.ci_lab.shadow"
    done = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, check=False)
    assert (done.returncode, done.stderr) == (0, "")


@pytest.mark.feature("lab-shadow-comparison")
def test_shards_fold_into_one_row_that_counts_each_turn_into_a_mismatch(
    recorder: shadow.Shadow, client: fakeredis.FakeRedis, blobs: LocalObjects, spool: Spool, registry: metrics.Registry
) -> None:
    assert recorder.observe(lab("tests")) is None
    assert recorder.observe(delivery("unit-selected (3.12, 1)")) == shadow.Row(
        HEAD, "tests", "g2", "pass", "pass", False
    )
    failed = shadow.Row(HEAD, "tests", "g2", "pass", "fail", True)
    assert recorder.observe(delivery("unit-selected (3.12, 2)", "failure")) == failed
    assert recorder.observe(delivery("unit-selected (3.12, 2)")) == shadow.Row(
        HEAD, "tests", "g2", "pass", "pass", False
    )
    assert recorder.observe(delivery("unit-selected (3.12, 3)", "timed_out")) == failed
    assert recorder.observe(delivery("unit-selected (3.12, 1)", "failure")) == failed
    assert registry.values[contract.VERDICT_MISMATCH_TOTAL] == {(("gate", "tests"),): 2.0}
    assert client.hgetall(f"agentihooks-ci:shadow:tests:{HEAD}") == {
        "lab:lab-shadow/tests": "pass",
        "grader": "g2",
        "github-actions:unit-selected (3.12, 1)": "fail",
        "github-actions:unit-selected (3.12, 2)": "pass",
        "github-actions:unit-selected (3.12, 3)": "fail",
        "head": HEAD,
        "gate": "tests",
        "lab_verdict": "pass",
        "actions_verdict": "fail",
        "mismatch": "1",
    }
    folder = f"lab/shadow/tests/{HEAD}/"
    assert blobs.keys("lab/shadow/") == [f"{folder}pass-fail.json", f"{folder}pass-pass.json"]
    assert blobs.read(f"{folder}pass-fail.json") == document(HEAD, "tests", "g2", "pass", "fail")
    assert blobs.read(f"{folder}pass-pass.json") == document(HEAD, "tests", "g2", "pass", "pass")
    match, mismatch = table_row(HEAD, "tests", "g2", "pass", "pass"), table_row(HEAD, "tests", "g2", "pass", "fail")
    assert spool.rows == [match, mismatch, match, mismatch, mismatch]


def test_a_lab_failure_of_one_static_step_against_a_green_actions_job_is_a_mismatch(
    recorder: shadow.Shadow, client: fakeredis.FakeRedis, registry: metrics.Registry
) -> None:
    assert recorder.observe(delivery("lint", head=OTHER)) is None
    passed = shadow.Row(OTHER, "lint", "g3", "pass", "pass", False)
    assert recorder.observe(lab("lint/format-check", head=OTHER, grader="g3")) == passed
    failed = shadow.Row(OTHER, "lint", "g3", "fail", "pass", True)
    assert recorder.observe(lab("lint/lint-check", "failure", head=OTHER, grader="g3")) == failed
    assert registry.values[contract.VERDICT_MISMATCH_TOTAL] == {(("gate", "lint"),): 1.0}
    assert client.hget(f"agentihooks-ci:shadow:lint:{OTHER}", "lab:lab-shadow/lint/lint-check") == "fail"
    assert shadow.rows(client) == [failed]


def test_deliveries_that_carry_no_verdict_record_nothing(recorder: shadow.Shadow, client: fakeredis.FakeRedis) -> None:
    quiet = [
        delivery("lint", action="created"),
        delivery("docs", action="created"),
        lab("lint", action="created"),
        delivery("lint", slug="sonarqubecloud"),
        delivery("docs", slug="sonarqubecloud"),
        delivery("lab-shadow/lint"),
        delivery("lab-shadow/docs"),
        delivery("lab-shadow/lint", slug="sonarqubecloud"),
        delivery("lint", "skipped"),
        delivery("lint", "cancelled"),
        delivery("lint", "neutral"),
        delivery("lint", None),
        lab("lint", "skipped"),
    ]
    assert [recorder.observe(payload) for payload in quiet] == [None] * len(quiet)
    assert client.keys("*") == []
    assert recorder.observe(delivery("docs")) is None
    assert recorder.observe(lab("docs")) is None
    assert client.keys("*") == ["agentihooks-ci:shadow-unmapped"]
    assert client.smembers("agentihooks-ci:shadow-unmapped") == {"docs", "lab-shadow/docs"}


def test_the_lab_check_run_summary_names_the_grader_revision() -> None:
    assert shadow.grader_revision(lab("lint", grader="abc123")["check_run"]["output"]["summary"]) == "abc123"
    assert shadow.grader_revision("Run: 1\nGrader revision: def456\nDetails: x") == "def456"
    assert shadow.grader_revision("Run: 1\nthe Grader revision: def456") == ""
    assert shadow.grader_revision("Grader revision: def456 extra") == ""
    assert shadow.grader_revision("") == ""
    assert shadow.grader_revision(None) == ""


def test_a_lab_summary_without_a_grader_revision_records_an_empty_one(recorder: shadow.Shadow) -> None:
    recorder.observe(delivery("lab-shadow/lint", slug=LAB_APP, summary="Shadow grade"))
    assert recorder.observe(delivery("lint")) == shadow.Row(HEAD, "lint", "", "pass", "pass", False)


def test_a_match_is_recorded_without_counting_a_mismatch(
    recorder: shadow.Shadow, client: fakeredis.FakeRedis, blobs: LocalObjects, spool: Spool, registry: metrics.Registry
) -> None:
    row = recorder.record(OTHER, "semgrep/scan", "fail", "fail", "g4")
    assert row == shadow.Row(OTHER, "semgrep/scan", "g4", "fail", "fail", False)
    assert registry.values[contract.VERDICT_MISMATCH_TOTAL] == {}
    assert client.hget(f"agentihooks-ci:shadow:semgrep/scan:{OTHER}", "mismatch") == "0"
    path = f"lab/shadow/semgrep/scan/{OTHER}/fail-fail.json"
    assert blobs.read(path) == document(OTHER, "semgrep/scan", "g4", "fail", "fail")
    assert spool.rows == [table_row(OTHER, "semgrep/scan", "g4", "fail", "fail")]
    assert shadow.rows(client) == [row]


def test_the_default_clock_stamps_rows_in_milliseconds(
    monkeypatch: pytest.MonkeyPatch, client: fakeredis.FakeRedis, blobs: LocalObjects, spool: Spool
) -> None:
    monkeypatch.setattr(shadow.time, "time_ns", lambda: 1_700_000_000_123_456_789)
    recorder = shadow.Shadow(client, blobs, spool, metrics.Registry(), shadow.Sources(STATIC, LAB_APP))
    recorder.record(HEAD, "lint", "pass", "pass", "g1")
    assert spool.rows[0][1]["ts_ms"] == 1_700_000_000_123


@pytest.mark.feature("lab-shadow-comparison")
def test_the_report_counts_heads_compared_and_mismatches_per_gate_since_a_grader_revision() -> None:
    order = {"g1": 1, "g2": 2, "g3": 3}

    def descends(older: str, newer: str) -> bool:
        return order.get(newer, 0) >= order[older]

    found = [
        shadow.Row("h1", "tests", "g2", "pass", "pass", False),
        shadow.Row("h1", "lint", "g2", "pass", "fail", True),
        shadow.Row("h2", "tests", "g3", "fail", "pass", True),
        shadow.Row("h2", "mutation", "g3", "pass", "pass", False),
        shadow.Row("h3", "tests", "g1", "fail", "pass", True),
        shadow.Row("h4", "lint", "", "pass", "pass", False),
    ]
    assert shadow.report(found, {"sonar", "docs"}, "g2", descends) == (
        "since g2\n"
        "heads compared 2\n"
        "gate lint compared 1 mismatches 1\n"
        "gate mutation compared 1 mismatches 0\n"
        "gate tests compared 2 mismatches 1\n"
        "unmapped docs\n"
        "unmapped sonar\n"
    )
    assert shadow.report(found, (), "g1", descends) == (
        "since g1\n"
        "heads compared 3\n"
        "gate lint compared 1 mismatches 1\n"
        "gate mutation compared 1 mismatches 0\n"
        "gate tests compared 3 mismatches 2\n"
    )
    assert shadow.report(found, (), "g3", lambda older, newer: False) == "since g3\nheads compared 0\n"


def test_ancestor_follows_the_git_history_quietly(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    repo, first, second = history(tmp_path)
    capfd.readouterr()
    assert shadow.ancestor(repo, first, second) is True
    assert shadow.ancestor(repo, first, first) is True
    assert shadow.ancestor(repo, second, first) is False
    assert shadow.ancestor(repo, first, "") is False
    assert shadow.ancestor(repo, "0" * 40, second) is False
    assert capfd.readouterr() == ("", "")


@pytest.mark.feature("lab-shadow-comparison")
def test_the_report_command_reads_the_lab_redis(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    recorder: shadow.Shadow,
    client: fakeredis.FakeRedis,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import redis

    def from_url(url: str, **options: object) -> fakeredis.FakeRedis:
        assert (url, options) == (URL, {"decode_responses": True})
        return client

    monkeypatch.setattr(redis.Redis, "from_url", from_url)
    repo, first, second = history(tmp_path)
    recorder.record("h1", "tests", "fail", "pass", first)
    recorder.record("h2", "tests", "pass", "pass", second)
    recorder.record("h2", "lint", "pass", "fail", second)
    recorder.observe(delivery("docs"))
    assert cli.main(["shadow", "report", "--since", second, "--repo", str(repo), "--redis", URL], {}) == 0
    assert capsys.readouterr().out == (
        f"since {second}\n"
        "heads compared 1\n"
        "gate lint compared 1 mismatches 1\n"
        "gate tests compared 1 mismatches 0\n"
        "unmapped docs\n"
    )
    monkeypatch.chdir(repo)
    assert cli.main(["shadow", "report", "--since", first], {"CI_LAB_REDIS_URL": URL}) == 0
    assert capsys.readouterr().out == (
        f"since {first}\n"
        "heads compared 2\n"
        "gate lint compared 1 mismatches 1\n"
        "gate tests compared 2 mismatches 1\n"
        "unmapped docs\n"
    )


def test_the_report_command_refuses_a_since_revision_the_repository_lacks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _, _ = history(tmp_path)
    unknown = "0" * 40
    with pytest.raises(SystemExit) as refused:
        cli.main(["shadow", "report", "--since", unknown, "--repo", str(repo), "--redis", URL], {})
    assert refused.value.code == 2
    assert capsys.readouterr().err.endswith(f"error: --since {unknown} names no commit in {repo}\n")


def test_the_report_command_refuses_without_a_lab_redis(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as refused:
        cli.main(["shadow", "report", "--since", "g1"], {})
    assert refused.value.code == 2
    error = capsys.readouterr().err
    assert error.startswith("usage: python -m scripts.ci_lab shadow report")
    assert error.endswith("error: --redis or CI_LAB_REDIS_URL names the lab Redis\n")
    with pytest.raises(SystemExit) as bare:
        cli.main(["shadow"], {})
    assert bare.value.code == 2
    capsys.readouterr()
    with pytest.raises(SystemExit) as sinceless:
        cli.main(["shadow", "report", "--redis", URL], {})
    assert sinceless.value.code == 2
    assert capsys.readouterr().err.endswith("error: the following arguments are required: --since\n")


@pytest.mark.feature("lab-shadow-reusable-caller-pairing")
def test_the_recorder_pairs_bare_semgrep_with_every_reusable_actions_job(
    client: fakeredis.FakeRedis, blobs: LocalObjects, spool: Spool, registry: metrics.Registry
) -> None:
    sources = shadow.Sources(shadow.static_jobs(ROOT), LAB_APP)
    recorder = shadow.Shadow(client, blobs, spool, registry, sources, lambda: NOW_MS)
    for child in ["scan", "feature-tests", "charts"]:
        assert recorder.observe(delivery(f"semgrep / {child}")) is None
    passed = shadow.Row(HEAD, "semgrep", "g3", "pass", "pass", False)
    assert recorder.observe(lab("semgrep", grader="g3")) == passed
    assert client.smembers(shadow.UNMAPPED_KEY) == set()
    failed = shadow.Row(HEAD, "semgrep", "g3", "pass", "fail", True)
    assert recorder.observe(delivery("semgrep / charts", "failure")) == failed
    assert shadow.rows(client) == [failed]
    assert registry.values[contract.VERDICT_MISMATCH_TOTAL] == {(("gate", "semgrep"),): 1.0}
