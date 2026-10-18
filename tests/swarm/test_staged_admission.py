import copy
import inspect
import json
import signal
import subprocess
from pathlib import Path

import pytest

from scripts.swarm import ledger_events, merge_queue, ready
from scripts.swarm.staged_compatibility import MARKER
from scripts.swarm.store import AgentRecord, SwarmError
from tests.swarm.test_merge_queue import ENTRY, OPEN, URL, checked_resource, runner

pytestmark = pytest.mark.unit

CANDIDATE = "https://github.com/o/r/pull/8"
RUN = "https://github.com/o/r/actions/runs/42"
HEAD = "a" * 40
CANDIDATE_HEAD = "b" * 40


READ_PULL = ledger_events.view


@pytest.fixture(autouse=True)
def read_real_pull(monkeypatch):
    monkeypatch.setattr(ledger_events, "view", READ_PULL)


def declaration(family):
    return {
        "complete": True,
        "prerequisite": {"url": URL, "head": HEAD},
        "candidate": {"url": CANDIDATE, "head": CANDIDATE_HEAD},
        "run": RUN,
        "contracts": [family],
    }


def body(proof):
    return MARKER + "\n" + json.dumps(proof) + "\n-->"


def staged(family):
    proof = declaration(family)
    endpoints = {
        "repos/o/r/pulls/7": {"state": "open", "head": {"sha": HEAD}, "merged": False},
        "repos/o/r/pulls/8": {"state": "open", "head": {"sha": CANDIDATE_HEAD}, "merged": False},
        f"repos/o/r/compare/{HEAD}...{CANDIDATE_HEAD}": {"status": "ahead"},
        "repos/o/r/actions/runs/42": {"status": "completed", "conclusion": "success", "head_sha": CANDIDATE_HEAD},
        "repos/o/r/actions/runs/42/jobs?per_page=100&page=1": {
            "total_count": 1,
            "jobs": [
                {"id": 1, "name": family, "status": "completed", "conclusion": "success", "head_sha": CANDIDATE_HEAD}
            ],
        },
    }
    return proof, endpoints


def queue(proof, endpoints, *, candidate=False, conclusion="SUCCESS"):
    url, head = (CANDIDATE, CANDIDATE_HEAD) if candidate else (URL, HEAD)
    raw = copy.deepcopy(OPEN)
    raw["headRefOid"], raw["body"] = head, body(proof)
    raw["mergeQueueEntry"] = None
    done = copy.deepcopy(raw)
    done["mergeQueueEntry"] = ENTRY
    base, calls = runner(
        {"data": {"resource": raw}}, checked_resource(conclusion, head), {"data": {}}, {"data": {"resource": done}}
    )

    def run(command, **kwargs):
        if command[2] in endpoints:
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, json.dumps(endpoints[command[2]]))
        if candidate:
            command = [
                part.replace(
                    ledger_events.PULL_QUERY.replace("$number", "8"), ledger_events.PULL_QUERY.replace("$number", "7")
                ).replace(CANDIDATE, URL)
                for part in command
            ]
        return base(command, **kwargs)

    return lambda: merge_queue.operate("queue", url, run=run), calls


@pytest.mark.parametrize("family", ["queue seed dependency assertion", "promotion squash method"])
@pytest.mark.parametrize(
    "fault,missing",
    [
        ("unfinished", "planned prerequisite edits"),
        ("prerequisite moved", "prerequisite head"),
        ("candidate moved", "candidate head"),
        ("stale run", "proof head"),
        ("red proof", "successful compatibility run"),
        ("running proof", "successful compatibility run"),
        ("missing family", "contract"),
        ("red family", "contract"),
        ("skipped family", "contract"),
        ("uncombined", "contain prerequisite"),
    ],
)
def test_queue_refuses_unfinished_or_stale_staged_compatibility(family, fault, missing):
    proof, endpoints = staged(family)
    if fault == "unfinished":
        proof["complete"] = False
    elif fault == "prerequisite moved":
        endpoints["repos/o/r/pulls/7"]["head"]["sha"] = "c" * 40
    elif fault == "candidate moved":
        endpoints["repos/o/r/pulls/8"]["head"]["sha"] = "c" * 40
    elif fault == "stale run":
        endpoints["repos/o/r/actions/runs/42"]["head_sha"] = "c" * 40
    elif fault == "red proof":
        endpoints["repos/o/r/actions/runs/42"]["conclusion"] = "failure"
    elif fault == "running proof":
        endpoints["repos/o/r/actions/runs/42"]["status"] = "in_progress"
    elif fault == "missing family":
        proof["contracts"].append("outside the initially edited module")
    elif fault in ("red family", "skipped family"):
        endpoints["repos/o/r/actions/runs/42/jobs?per_page=100&page=1"]["jobs"][0]["conclusion"] = (
            "failure" if fault == "red family" else "skipped"
        )
    else:
        endpoints[f"repos/o/r/compare/{HEAD}...{CANDIDATE_HEAD}"]["status"] = "diverged"
    invoke, calls = queue(proof, endpoints)
    with pytest.raises(SwarmError, match=missing):
        invoke()
    assert not any(f"query={merge_queue.QUEUE}" in cmd for cmd, _ in calls)


@pytest.mark.parametrize("family", ["queue seed dependency assertion", "promotion squash method"])
def test_complete_exact_head_prerequisite_queues_with_all_existing_checks(family):
    proof, endpoints = staged(family)
    invoke, calls = queue(proof, endpoints)
    assert invoke()["queued"]
    assert any(f"query={merge_queue.QUEUE}" in cmd and f"head={HEAD}" in cmd for cmd, _ in calls)
    invoke, calls = queue(proof, endpoints, conclusion="FAILURE")
    with pytest.raises(SwarmError, match="CI Validate"):
        invoke()
    assert not any(f"query={merge_queue.QUEUE}" in cmd for cmd, _ in calls)


def test_candidate_waits_for_the_exact_prerequisite_to_merge():
    proof, endpoints = staged("promotion squash method")
    invoke, calls = queue(proof, endpoints, candidate=True)
    with pytest.raises(SwarmError, match="prerequisite.*merge"):
        invoke()
    assert not any(f"query={merge_queue.QUEUE}" in cmd for cmd, _ in calls)
    endpoints["repos/o/r/pulls/7"]["merged"] = True
    invoke, _ = queue(proof, endpoints, candidate=True)
    assert invoke()["queued"]


def test_ready_refuses_unfinished_staged_work_before_releasing_owner(monkeypatch):
    proof = declaration("queue seed dependency assertion")
    proof["complete"] = False
    pull = type("Pull", (), {"head": HEAD, "state": "OPEN", "body": body(proof), "queued": False})()
    agent = AgentRecord(name="ci-1", lane="ci", profile="cicd", task="t1")
    monkeypatch.setattr(ready.stack, "shell", lambda cmd: subprocess.CompletedProcess(cmd, 0, "false"))
    monkeypatch.setattr(ready, "_checkout", lambda *args: "/worktree")
    with pytest.raises(SwarmError, match="planned prerequisite edits"):
        ready.check({"kind": "ci", "pr_url": URL}, agent, "full", lambda url: pull, 1)


def test_pull_request_view_retains_the_declaration_for_ready():
    proof = body(declaration("promotion squash method"))
    pull = ledger_events.pull_request({"state": "OPEN", "body": proof})
    assert pull.body == proof
    assert ledger_events.pull_request({"state": "OPEN"}).body == ""


def test_priority_refuses_unfinished_compatibility_before_displacing_entries():
    from tests.swarm.test_merge_queue_priority import URL as PRIORITY_URL
    from tests.swarm.test_merge_queue_priority import FakeQueue

    fake = FakeQueue()
    proof = declaration("promotion squash method")
    proof["complete"] = False

    def run(command, **kwargs):
        done = fake.run(command, **kwargs)
        data = json.loads(done.stdout)
        if command[2] == "graphql" and f"query={merge_queue.STATE}" in command:
            data["data"]["resource"]["body"] = body(proof)
        return subprocess.CompletedProcess(command, 0, json.dumps(data))

    with pytest.raises(SwarmError, match="planned prerequisite edits"):
        merge_queue.prioritize(PRIORITY_URL, run=run)
    assert fake.mutations == []


def test_priority_never_restores_an_unfinished_prerequisite():
    from tests.swarm.test_merge_queue_priority import URL as PRIORITY_URL
    from tests.swarm.test_merge_queue_priority import FakeQueue

    fake = FakeQueue()
    proof = declaration("queue seed dependency assertion")
    proof["complete"] = False

    def run(command, **kwargs):
        done = fake.run(command, **kwargs)
        data = json.loads(done.stdout)
        if (
            command[2] == "graphql"
            and f"query={merge_queue.STATE}" in command
            and "url=https://github.com/o/r/pull/2" in command
        ):
            data["data"]["resource"]["body"] = body(proof)
        return subprocess.CompletedProcess(command, 0, json.dumps(data))

    result = merge_queue.prioritize(PRIORITY_URL, run=run)
    assert ("queue", 2) not in fake.mutations
    assert ("queue", 3) in fake.mutations
    assert result["displaced"] == ["https://github.com/o/r/pull/3"]
    assert len(result["errors"]) == 1
    assert "planned prerequisite edits" in result["errors"][0]


@pytest.mark.parametrize(
    "change",
    [
        {"prerequisite": None},
        {"prerequisite": {"url": URL, "head": "short"}},
        {"candidate": {"url": "https://github.com/other/repo/pull/8", "head": CANDIDATE_HEAD}},
        {"candidate": {"url": URL, "head": CANDIDATE_HEAD}},
        {"run": "https://github.com/other/repo/actions/runs/42"},
        {"contracts": []},
        {"contracts": [""]},
        {"contracts": [None]},
    ],
)
def test_malformed_declarations_fail_closed(change):
    proof, endpoints = staged("promotion squash method")
    proof.update(change)
    invoke, calls = queue(proof, endpoints)
    with pytest.raises(SwarmError, match="staged compatibility"):
        invoke()
    assert not any(f"query={merge_queue.QUEUE}" in cmd for cmd, _ in calls)


@pytest.mark.parametrize(
    "bad_body,diagnostic",
    [
        ("<!-- staged-compatibility", "staged compatibility: provide one complete declaration"),
        ("<!-- staged-compatibility invalid -->", "staged compatibility: declaration must be valid JSON"),
        (
            "<!-- staged-compatibility [] -->",
            "staged compatibility: finish all planned prerequisite edits and set complete to true",
        ),
        (
            "<!-- staged-compatibility {} --><!-- staged-compatibility {} -->",
            "staged compatibility: provide one complete declaration",
        ),
        (
            '<!-- staged-compatibility {"complete": false} -->',
            "staged compatibility: finish all planned prerequisite edits and set complete to true",
        ),
    ],
)
def test_unreadable_declaration_keeps_the_ready_owner(monkeypatch, bad_body, diagnostic):
    pull = type("Pull", (), {"head": HEAD, "state": "OPEN", "body": bad_body, "queued": False})()
    agent = AgentRecord(name="ci-1", lane="ci", task="t1")
    monkeypatch.setattr(ready.stack, "shell", lambda cmd: subprocess.CompletedProcess(cmd, 0, "false"))
    monkeypatch.setattr(ready, "_checkout", lambda *args: pytest.fail("unfinished proof reached checkout release"))
    with pytest.raises(SwarmError) as error:
        ready.check({"kind": "ci", "pr_url": URL}, agent, "full", lambda url: pull, 1)
    assert str(error.value) == diagnostic


def test_compatibility_reads_all_job_pages():
    proof, endpoints = staged("promotion squash method")
    job = endpoints["repos/o/r/actions/runs/42/jobs?per_page=100&page=1"]["jobs"][0]
    endpoints["repos/o/r/actions/runs/42/jobs?per_page=100&page=1"] = {
        "total_count": 101,
        "jobs": [{**job, "id": number, "name": "unrelated"} for number in range(100)],
    }
    endpoints["repos/o/r/actions/runs/42/jobs?per_page=100&page=2"] = {
        "total_count": 101,
        "jobs": [{**job, "id": 100}],
    }
    invoke, _ = queue(proof, endpoints)
    assert invoke()["queued"]


@pytest.mark.parametrize(
    "change",
    [
        {"total_count": 2},
        {"jobs": []},
        {
            "jobs": [
                {
                    "id": 1,
                    "name": "promotion squash method",
                    "status": "in_progress",
                    "conclusion": "success",
                    "head_sha": CANDIDATE_HEAD,
                }
            ]
        },
        {
            "jobs": [
                {
                    "id": 1,
                    "name": "promotion squash method",
                    "status": "completed",
                    "conclusion": "success",
                    "head_sha": HEAD,
                }
            ]
        },
    ],
)
def test_incomplete_or_stale_job_inventory_never_admits(change):
    proof, endpoints = staged("promotion squash method")
    endpoints["repos/o/r/actions/runs/42/jobs?per_page=100&page=1"].update(change)
    invoke, calls = queue(proof, endpoints)
    with pytest.raises(SwarmError, match="staged compatibility"):
        invoke()
    assert not any(f"query={merge_queue.QUEUE}" in cmd for cmd, _ in calls)


def test_staged_publisher_proof_includes_the_old_queue_seed_dependency_assertion(monkeypatch):
    from tests import test_ci_queue_seed

    candidate = copy.deepcopy(test_ci_queue_seed._jobs())
    job = candidate["coverage-baseline"]
    job["needs"] = ["unit", "gate-required"]
    base = copy.deepcopy(candidate)
    job["needs"] = ["unit", "coverage-ratchet"]
    assert base["coverage-baseline"]["needs"] == ["unit", "gate-required"]
    assert "needs.unit.result == 'success'" in job["if"]
    contract = test_ci_queue_seed.test_failed_units_cannot_publish_a_push_baseline
    source = inspect.getsource(contract)
    legacy = source.replace(
        'assert job["needs"] in (["unit", "gate-required"], ["unit", "coverage-ratchet"])',
        'assert job["needs"] == ["unit", "gate-required"]',
    )
    assert legacy != source
    namespace = {"_jobs": lambda: candidate}
    exec(compile(legacy, "staged_queue_seed_contract.py", "exec"), namespace)
    with pytest.raises(AssertionError):
        namespace[contract.__name__]()

    proof, endpoints = staged(contract.__name__)
    evidence = endpoints["repos/o/r/actions/runs/42/jobs?per_page=100&page=1"]["jobs"][0]
    evidence["conclusion"] = "failure"
    invoke, _ = queue(proof, endpoints)
    with pytest.raises(SwarmError, match=contract.__name__):
        invoke()

    monkeypatch.setattr(test_ci_queue_seed, "_jobs", lambda: candidate)
    contract()
    evidence["conclusion"] = "success"
    invoke, _ = queue(proof, endpoints)
    assert invoke()["queued"]

    job["if"] = "always()"
    with pytest.raises(AssertionError):
        contract()
    evidence["conclusion"] = "failure"
    invoke, _ = queue(proof, endpoints)
    with pytest.raises(SwarmError, match=contract.__name__):
        invoke()


def test_staged_promotion_proof_accepts_squash_and_preserves_the_exact_merge_request(tmp_path):
    root = Path(__file__).resolve().parents[2]
    source = (root / "tests/contracts/promotion/promote.cjs").read_text().split("\ntest(", 1)[0]
    source = source.replace(
        "require('../../../.github/scripts/promote.cjs')",
        "require(" + json.dumps(str(root / ".github/scripts/promote.cjs")) + ")",
    )
    script = tmp_path / "staged-promotion.cjs"
    script.write_text(
        source
        + """
(async () => {
  const f = fixture();
  await promote(f);
  assert.equal(f.writes[1][0], 'merge');
  const { merge_method, ...request } = f.writes[1][1];
  if (process.argv[2] === 'old') assert.equal(merge_method, 'rebase');
  else assert.ok(['rebase', 'squash'].includes(merge_method));
  assert.deepEqual(request, {
    owner: 'owner', repo: 'repo', pull_number: 7, sha: 'snapshot',
    commit_message: f.writes[0][1].body,
  });
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    )
    old = subprocess.run(["node", str(script), "old"], capture_output=True, text=True)
    assert old.returncode == 1
    assert "rebase" in old.stderr and "squash" in old.stderr
    proof, endpoints = staged("promotion API contracts")
    evidence = endpoints["repos/o/r/actions/runs/42/jobs?per_page=100&page=1"]["jobs"][0]
    evidence["conclusion"] = "failure"
    invoke, _ = queue(proof, endpoints)
    with pytest.raises(SwarmError, match="promotion API contracts"):
        invoke()

    compatible = subprocess.run(["node", str(script), "compatible"], capture_output=True, text=True)
    assert compatible.returncode == 0, compatible.stdout + compatible.stderr
    evidence["conclusion"] = "success"
    invoke, _ = queue(proof, endpoints)
    assert invoke()["queued"]


def evidence_runner(responses, endpoints):
    base, calls = runner(*responses)

    def run(command, **kwargs):
        if command[2] in endpoints:
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, json.dumps(endpoints[command[2]]))
        return base(command, **kwargs)

    return run, calls


@pytest.mark.parametrize("suffix", ["", "\n<!-- unrelated metadata -->"])
def test_ready_accepts_complete_proof_on_its_observed_head(monkeypatch, suffix):
    proof, endpoints = staged("promotion API contracts")
    pull = ledger_events.pull_request({"state": "OPEN", "headRefOid": HEAD, "body": body(proof) + suffix})
    agent = AgentRecord(name="ci-1", lane="ci", task="t1")
    monkeypatch.setattr(ready.stack, "shell", lambda cmd: subprocess.CompletedProcess(cmd, 0, "false"))
    monkeypatch.setattr(ready, "_checkout", lambda *args: "/worktree")
    monkeypatch.setattr(merge_queue, "rest", lambda path, run: endpoints[path])
    found = ready.check({"kind": "ci", "pr_url": URL}, agent, "full", lambda url: pull, 1)
    assert found["head"] == HEAD
    assert found["url"] == URL


def test_priority_accepts_complete_exact_head_proof(monkeypatch):
    proof, endpoints = staged("promotion API contracts")
    raw = {**OPEN, "headRefOid": HEAD, "body": body(proof)}
    run, calls = evidence_runner(
        [
            {"data": {"resource": raw}},
            checked_resource(head=HEAD),
            {"data": {"resource": raw}},
            {"data": {}},
        ],
        endpoints,
    )
    monkeypatch.setattr(merge_queue, "waiting_ahead", lambda *args: [])
    assert merge_queue.prioritize(URL, run=run)["errors"] == []
    assert any(f"query={merge_queue.QUEUE}" in cmd and f"head={HEAD}" in cmd for cmd, _ in calls)


def test_priority_restores_a_completed_staged_prerequisite_on_its_original_head(monkeypatch):
    other = "https://github.com/o/r/pull/2"
    proof, endpoints = staged("queue seed dependency assertion")
    proof["prerequisite"]["url"] = other
    endpoints["repos/o/r/pulls/2"] = endpoints.pop("repos/o/r/pulls/7")
    staged_raw = {**OPEN, "id": "PR_two", "headRefOid": HEAD, "body": body(proof)}
    run, calls = evidence_runner(
        [
            {"data": {"resource": OPEN}},
            checked_resource(),
            {"data": {"resource": {**staged_raw, "mergeQueueEntry": {**ENTRY, "state": "QUEUED"}}}},
            {"data": {}},
            {"data": {"resource": OPEN}},
            {"data": {}},
            {"data": {"resource": staged_raw}},
            {"data": {}},
        ],
        endpoints,
    )
    monkeypatch.setattr(
        merge_queue,
        "waiting_ahead",
        lambda *args: [
            {"id": "PR_two", "url": other, "headRefOid": HEAD},
        ],
    )
    result = merge_queue.prioritize(URL, run=run)
    assert result["errors"] == []
    assert result["displaced"] == [other]
    assert any(f"query={merge_queue.QUEUE}" in cmd and "id=PR_two" in cmd and f"head={HEAD}" in cmd for cmd, _ in calls)


@pytest.mark.parametrize(
    "role,field",
    [
        ("prerequisite", "url"),
        ("candidate", "url"),
        ("prerequisite", "head"),
        ("candidate", "head"),
    ],
)
def test_missing_target_fields_name_the_incomplete_role(role, field):
    proof, endpoints = staged("promotion API contracts")
    del proof[role][field]
    invoke, _ = queue(proof, endpoints)
    with pytest.raises(SwarmError, match=f"valid {role} URL and full head"):
        invoke()


def test_invalid_full_head_names_the_required_correction():
    proof, endpoints = staged("promotion API contracts")
    proof["prerequisite"]["head"] = "short"
    invoke, _ = queue(proof, endpoints)
    with pytest.raises(SwarmError, match="valid prerequisite URL and full head"):
        invoke()


def test_missing_run_names_the_required_proof():
    proof, endpoints = staged("promotion API contracts")
    del proof["run"]
    invoke, _ = queue(proof, endpoints)
    with pytest.raises(SwarmError, match="proof run must share a repository"):
        invoke()


def test_a_declaration_for_a_different_observed_head_names_that_gap():
    proof, endpoints = staged("promotion API contracts")
    proof["prerequisite"]["head"] = "c" * 40
    invoke, _ = queue(proof, endpoints)
    with pytest.raises(SwarmError, match="exact pull request head"):
        invoke()


def test_identical_candidate_and_prerequisite_commits_have_valid_ancestry():
    proof, endpoints = staged("promotion API contracts")
    proof["candidate"]["head"] = HEAD
    endpoints["repos/o/r/pulls/8"]["head"]["sha"] = HEAD
    endpoints["repos/o/r/actions/runs/42"]["head_sha"] = HEAD
    endpoints["repos/o/r/actions/runs/42/jobs?per_page=100&page=1"]["jobs"][0]["head_sha"] = HEAD
    endpoints[f"repos/o/r/compare/{HEAD}...{HEAD}"] = {"status": "identical"}
    invoke, _ = queue(proof, endpoints)
    assert invoke()["queued"]


def test_nonadvancing_full_job_pages_fail_without_another_page_request():
    proof, endpoints = staged("promotion API contracts")
    job = endpoints["repos/o/r/actions/runs/42/jobs?per_page=100&page=1"]["jobs"][0]
    repeated = {"total_count": 101, "jobs": [{**job, "id": n} for n in range(100)]}
    endpoints["repos/o/r/actions/runs/42/jobs?per_page=100&page=1"] = repeated
    endpoints["repos/o/r/actions/runs/42/jobs?per_page=100&page=2"] = repeated
    invoke, calls = queue(proof, endpoints)
    with pytest.raises(SwarmError, match="incomplete compatibility job inventory"):
        invoke()
    assert not any("page=3" in cmd[2] for cmd, _ in calls)


@pytest.mark.parametrize("response", [{}, None])
def test_unreadable_proof_response_names_the_repair(response):
    proof, endpoints = staged("promotion API contracts")
    endpoints["repos/o/r/actions/runs/42"] = response
    invoke, _ = queue(proof, endpoints)
    with pytest.raises(SwarmError, match="unreadable compatibility proof"):
        invoke()


def test_ready_bounds_unterminated_declaration_parsing(monkeypatch):
    pull = ledger_events.pull_request({"state": "OPEN", "headRefOid": HEAD, "body": MARKER + " " * 65_000})
    agent = AgentRecord(name="ci-1", lane="ci", task="t1")
    monkeypatch.setattr(ready.stack, "shell", lambda cmd: subprocess.CompletedProcess(cmd, 0, "false"))
    monkeypatch.setattr(ready, "_checkout", lambda *args: pytest.fail("unfinished proof reached checkout release"))

    def deadline(signum, frame):
        raise TimeoutError("declaration parsing exceeded three seconds")

    previous_handler = signal.signal(signal.SIGALRM, deadline)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 3)
    try:
        with pytest.raises(SwarmError, match="provide one complete declaration"):
            ready.check({"kind": "ci", "pr_url": URL}, agent, "full", lambda url: pull, 1)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def test_ready_preserves_unicode_whitespace_after_the_declaration_marker(monkeypatch):
    proof, endpoints = staged("promotion API contracts")
    declaration_body = MARKER + "\u00a0" + json.dumps(proof) + "-->"
    pull = ledger_events.pull_request({"state": "OPEN", "headRefOid": HEAD, "body": declaration_body})
    agent = AgentRecord(name="ci-1", lane="ci", task="t1")
    monkeypatch.setattr(ready.stack, "shell", lambda cmd: subprocess.CompletedProcess(cmd, 0, "false"))
    monkeypatch.setattr(ready, "_checkout", lambda *args: "/worktree")
    monkeypatch.setattr(merge_queue, "rest", lambda path, run: endpoints[path])
    assert ready.check({"kind": "ci", "pr_url": URL}, agent, "full", lambda url: pull, 1)["head"] == HEAD
