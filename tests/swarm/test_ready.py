import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts.gates import Call, Who
from scripts.gates.base import Decision
from scripts.gates.claim_stop import ClaimStop
from scripts.gates.verdicts import Verdicts
from scripts.swarm import capacity, cli, ready, stack, tick, waits
from scripts.swarm.ledger_events import PullRequest
from scripts.swarm.store import RedisStore, SwarmError
from tests.swarm.test_cli import HANDOFF, env

pytestmark = pytest.mark.xdist_group("fakeredis")
_fixture = env
AGENT = "engineer@a1b2c3-0001"
URL = "https://github.com/o/r/pull/7"
HEAD = "a" * 40
NOW = 5_000
ROOT = "/home/me/dev/worktrees"
TOP = f"{ROOT}/repo/engineer-a1b2c3-0001"
GREEN = PullRequest("OPEN", None, 1, False, True, head=HEAD)
RED = PullRequest("OPEN", None, 1, True, True, head=HEAD)
RUNNING = PullRequest("OPEN", None, 1, False, False, head=HEAD)
QUEUED = PullRequest("OPEN", None, 1, False, True, head=HEAD, queued=True)
MERGED = PullRequest("MERGED", 2, 1, False, True, head=HEAD)
CLOSED = PullRequest("CLOSED", None, 1, False, True, head=HEAD)
REMOVE = ["bash", str(stack.WT_SCRIPT), "done", "engineer-a1b2c3-0001", "--repo", TOP]


class Shell:
    def __init__(self):
        self.calls, self.status, self.head, self.draft, self.fail = [], "", HEAD, "false", ()

    def __call__(self, argv):
        self.calls.append(argv)
        out = {
            ("git", "rev-parse", "--show-toplevel"): TOP,
            ("git", "rev-parse", "--path-format=absolute"): "/home/me/dev/repo/.git",
            ("bash", str(stack.WT_SCRIPT), "root"): ROOT,
            ("git", "status", "--porcelain"): self.status,
            ("git", "rev-parse", "HEAD"): self.head,
            ("gh", "pr", "view"): self.draft,
        }.get(tuple(argv[:3]), "")
        code = 1 if tuple(argv[:3]) in self.fail else 0
        return subprocess.CompletedProcess(argv, code, stdout=out + "\n", stderr=" boom\n" if code else "")


@pytest.fixture
def released(env, monkeypatch, tmp_path):
    store, ledger, rt = env
    monkeypatch.setenv("HOME", str(tmp_path))
    cli.main(["sw", "create", "--repo", "/repo"])
    cli.main(["sw", "start"])
    ledger.rows["t1"].update({"state": "pr", "pr_url": URL, "branch": "eng-t1"})
    ledger.pulls[URL] = RUNNING
    ledger.tasks = lambda slug: list(ledger.rows.values()) if slug == "sw" else []
    ledger.updates, update = [], ledger.update_task
    ledger.update_task = lambda slug, task, fields, by="swarm", if_state=(): (
        ledger.updates.append((task, fields, by)) or update(slug, task, fields, by, if_state)
    )
    shell = Shell()
    monkeypatch.setattr(stack, "shell", shell)
    monkeypatch.setattr(cli, "now_ms", lambda: NOW)
    doc = tmp_path / "handoff.md"
    doc.write_text(HANDOFF.format(intent="Finish the parser task for the swarm."))
    return store, ledger, shell, doc


def release(doc):
    return cli.main(["sw", "--as", AGENT, "ready", str(doc)])


def working(store):
    return [a.state for a in store.agents("sw") if a.name == AGENT] == ["working"]


@pytest.mark.parametrize("kind", ["code", "ci"])
def test_ready_records_the_pull_request_and_head_and_ends_the_agent(released, capsys, kind):
    store, ledger, shell, doc = released
    ledger.rows["t1"]["kind"] = kind
    capsys.readouterr()
    assert release(doc) == 0
    assert ledger.rows["t1"]["state"] == "pr" and ledger.rows["t1"]["awaiting"] == "merge"
    assert ledger.updates[-1] == ("t1", {"state": "pr", "awaiting": "merge"}, AGENT)
    assert json.loads(store.redis.hget(store.key("sw", "ready"), "t1")) == {
        "url": URL,
        "head": HEAD,
        "worktree": TOP,
        "agent": AGENT,
        "at": NOW,
        "on": {"kind": "checks", "target": URL, "head": HEAD},
    }
    assert ["gh", "pr", "view", URL, "--json", "isDraft", "--jq", ".isDraft"] in shell.calls
    assert store.handoff("sw", "t1") == doc.read_text()
    assert store.handoff_envelope("sw", "t1")["reason"] == "exit"
    assert [a.state for a in store.agents("sw") if a.name == AGENT] == ["finished"]
    assert json.loads(capsys.readouterr().out) == {
        "task": "t1",
        "pr_url": URL,
        "head": HEAD,
        "awaiting": "merge",
        "next": "stop now; the swarm carries the pull request to merge and closes this session",
    }


def test_a_second_ready_is_refused_once_the_seat_is_released(released, capsys):
    store, _, _, doc = released
    assert release(doc) == 0
    first = ready.record(store, "sw", "t1")
    capsys.readouterr()
    assert release(doc) == 1
    assert capsys.readouterr().err == (
        f"swarm: {AGENT} already handed off its seat; the swarm carries its pull request\n"
    )
    assert ready.record(store, "sw", "t1") == first


def test_a_failed_handoff_withdraws_the_release(released, capsys, monkeypatch):
    store, ledger, _, doc = released

    def refused(*args):
        raise SwarmError("handoff store down")

    monkeypatch.setattr(cli, "_hand_off", refused)
    assert release(doc) == 1
    assert capsys.readouterr().err.endswith("handoff store down\n")
    assert ledger.rows["t1"]["awaiting"] == "" and ready.record(store, "sw", "t1") is None
    assert ledger.updates[-1] == ("t1", {"awaiting": ""}, AGENT)


def test_ready_resolves_read_first_addresses_against_this_swarm(released, tmp_path):
    store, _, _, doc = released
    store.memory.add_recap("eng-1@sw", "engineer@a1b2c3-0000", "t1", "an earlier recap", 1)
    folder = tmp_path / ".agentihooks" / "swarm" / "sw" / "tasks" / "t1"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "progress.md").write_text("- started\n")
    extra = "- recap:eng-1@sw the earlier recap\n- workspace:t1/progress the step log\n<!-- handoff complete -->"
    doc.write_text(doc.read_text().replace("<!-- handoff complete -->", extra))
    assert release(doc) == 0


def test_ready_refuses_a_handoff_document_it_cannot_read(released, capsys, tmp_path):
    store, _, _, _ = released
    missing = tmp_path / "nowhere.md"
    assert release(missing) == 1
    assert capsys.readouterr().err == (
        f"swarm: cannot read the handoff document {missing}: No such file or directory\n"
    )
    assert working(store)


def test_ready_refuses_a_task_gone_from_the_ledger(released, capsys):
    store, ledger, _, doc = released
    del ledger.rows["t1"]
    assert release(doc) == 1
    assert capsys.readouterr().err == "swarm: record the pull request with swarm pr before swarm ready\n"


def test_ready_refuses_a_task_under_the_controller_outcome_path(released, capsys):
    store, ledger, _, doc = released
    store.redis.set(store.key("sw", "task-authority", "t1"), 1)
    assert release(doc) == 1
    assert capsys.readouterr().err == "swarm: distributed final mutations require the controller outcome path\n"
    assert ready.record(store, "sw", "t1") is None and working(store)


def test_a_ready_task_no_longer_holds_a_busy_seat_and_is_never_reopened(released):
    store, ledger, _, doc = released
    assert capacity._busy(store.agents("sw"))["eng"] == 1
    assert release(doc) == 0
    assert capacity._busy(store.agents("sw"))["eng"] == 0
    rows = {t["id"]: t for t in ledger.tasks("sw")}
    store.release("sw", "t1", AGENT)
    store.drop_agent("sw", AGENT)
    assert tick._orphans("sw", store, ledger, rows) == []
    assert ledger.rows["t1"]["state"] == "pr" and ledger.rows["t1"]["claimed_by"] == AGENT


def test_the_reaper_retires_a_ready_agent_without_reopening_its_task(released):
    store, ledger, _, doc = released
    assert release(doc) == 0
    rows = {t["id"]: t for t in ledger.tasks("sw")}
    assert tick._reap("sw", store, ledger, cli.HerdrRuntime(), rows, NOW) == [f"retired {AGENT}"]
    assert ledger.rows["t1"]["state"] == "pr" and ledger.rows["t1"]["claimed_by"] == AGENT
    assert store.handoff("sw", "t1") == doc.read_text()


def test_the_stop_gate_lets_a_ready_engineer_stop(released, tmp_path):
    store, ledger, _, doc = released
    ledger.pulls[URL] = GREEN
    gate = ClaimStop(connect=lambda: store, ledger=lambda: ledger, github=lambda url: GREEN, now=lambda: NOW)
    who = Who(name=AGENT, swarm="sw", lane="eng", task="t1")

    def stop():
        return gate.decide(Call(""), who, Verdicts("sw", gate.name, tmp_path))

    assert not stop().allowed
    assert release(doc) == 0
    assert stop() == Decision()
    assert ledger.rows["t1"]["state"] == "pr"


@pytest.mark.parametrize(
    "change, says",
    [
        ({"pr_url": ""}, "record the pull request with swarm pr before swarm ready"),
        (
            {"kind": "ops"},
            "swarm ready carries a code or ci pull request; this ops task closes with swarm done and its proof",
        ),
    ],
)
def test_ready_refuses_a_task_without_a_pull_request_to_carry(released, capsys, change, says):
    store, ledger, _, doc = released
    ledger.rows["t1"].update(change)
    assert release(doc) == 1
    assert capsys.readouterr().err == f"swarm: {says}\n"
    assert ready.record(store, "sw", "t1") is None and store.handoff("sw", "t1") == ""
    assert working(store)


@pytest.mark.parametrize(
    "pull, says",
    [
        (None, f"cannot read pull request {URL} from GitHub; run swarm ready again when it answers"),
        (
            PullRequest("OPEN", None, 1, False, True, head=""),
            f"cannot read pull request {URL} from GitHub; run swarm ready again when it answers",
        ),
        (MERGED, f"pull request {URL} is merged; close the task with swarm done"),
        (CLOSED, f"pull request {URL} is closed; swarm ready carries an open pull request"),
    ],
)
def test_ready_refuses_a_pull_request_it_cannot_carry(released, capsys, pull, says):
    store, ledger, _, doc = released
    ledger.pulls[URL] = pull
    assert release(doc) == 1
    assert capsys.readouterr().err == f"swarm: {says}\n"
    assert ledger.rows["t1"].get("awaiting", "") == ""


def test_ready_refuses_a_draft_pull_request(released, capsys):
    store, _, shell, doc = released
    shell.draft = "true"
    assert release(doc) == 1
    assert capsys.readouterr().err == (
        f"swarm: pull request {URL} is a draft; mark it ready for review with gh pr ready first\n"
    )
    assert ready.record(store, "sw", "t1") is None and working(store)
    shell.fail = (("gh", "pr", "view"),)
    assert release(doc) == 1
    assert capsys.readouterr().err == (
        f"swarm: cannot read whether pull request {URL} is a draft; run swarm ready again when it answers\n"
    )


def test_ready_refuses_a_worktree_whose_head_the_pull_request_lacks(released, capsys):
    store, ledger, shell, doc = released
    shell.head = "b" * 40
    assert release(doc) == 1
    assert capsys.readouterr().err == (
        "swarm: the pull request head is not this worktree's head; push your last commit before swarm ready\n"
    )
    shell.head, shell.status = HEAD, " M scripts/x.py"
    assert release(doc) == 1
    assert capsys.readouterr().err == "swarm: the worktree holds uncommitted changes; commit and push them first\n"
    shell.status, shell.fail = "", (("git", "status", "--porcelain"),)
    assert release(doc) == 1
    assert capsys.readouterr().err == "swarm: git status failed: boom\n"
    assert ready.record(store, "sw", "t1") is None


@pytest.mark.parametrize("autonomy", ["manual", "assist"])
def test_ready_refuses_where_the_operator_approves_each_merge(released, capsys, autonomy):
    store, _, _, doc = released
    store.update("sw", autonomy=autonomy)
    assert release(doc) == 1
    assert capsys.readouterr().err == (
        f"swarm: this swarm runs at {autonomy} autonomy, where the operator approves each merge; "
        "follow the ship steps in your prompt\n"
    )


def test_ready_refuses_a_malformed_handoff(released, capsys):
    store, _, _, doc = released
    doc.write_text("# Handoff v2\n")
    assert release(doc) == 1
    assert capsys.readouterr().err.startswith("swarm: handoff refused; fix the document and run the command again:\n- ")
    assert ready.record(store, "sw", "t1") is None
    assert working(store)


class Ledger:
    def __init__(self, rows):
        self.rows, self.comments = rows, []

    def update_task(self, slug, task_id, fields, by="swarm", if_state=()):
        assert slug == "sw" and by == "swarm"
        row = self.rows[task_id]
        if not if_state or row["state"] in if_state:
            row.update(fields)
        return dict(row)

    def comment(self, slug, task_id, text, by):
        assert slug == "sw"
        self.comments.append((task_id, text, by))


@pytest.fixture
def merging(monkeypatch):
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    rows = {
        "t1": {"id": "t1", "state": "pr", "claimed_by": AGENT, "pr_url": URL, "awaiting": "merge"},
        "t2": {"id": "t2", "state": "open", "claimed_by": ""},
    }
    ledger = Ledger(rows)
    store.put_handoff("sw", "t1", "the handoff")
    store.claim("sw", "t1", AGENT, 60_000)
    ready.save(store, "sw", "t1", ready.held_record(URL, HEAD, TOP, AGENT, NOW))
    pulls, queued, shell = {URL: RUNNING}, [], Shell()
    monkeypatch.setattr(stack, "shell", shell)

    def queue(action, url):
        queued.append((action, url))
        return {"url": url, "queued": True, **getattr(queue, "extra", {})}

    def run(now=NOW, queuer=queue):
        return ready.merge_pass(ready.Pass(store, "sw", ledger, pulls.get, pulls.get, now, queuer), rows)

    return SimpleNamespace(
        store=store, rows=rows, ledger=ledger, pulls=pulls, queued=queued, shell=shell, run=run, queue=queue
    )


def queued_at_now(merging):
    merging.pulls[URL] = GREEN
    assert merging.run() == [f"queued {URL} for task t1"]


def test_the_merge_pass_waits_while_checks_run(merging):
    assert merging.run() == []
    assert merging.queued == [] and merging.rows["t1"]["state"] == "pr"


def test_the_merge_pass_queues_on_green_checks_and_watches_the_queue(merging):
    queued_at_now(merging)
    assert merging.queued == [("queue", URL)]
    assert ready.record(merging.store, "sw", "t1")["on"] == {"kind": "merge", "target": URL}
    assert ready.record(merging.store, "sw", "t1")["queued_at"] == NOW
    merging.pulls[URL] = QUEUED
    assert merging.run(NOW + 60_000) == []
    assert ready.record(merging.store, "sw", "t1")["on"] == {"kind": "merge", "target": URL, "queued": True}


def test_a_queue_that_updates_the_branch_watches_the_new_head_checks(merging):
    merging.pulls[URL] = GREEN
    merging.queue.extra = {"queued": False, "waiting": "checks", "previous_head": HEAD}
    assert merging.run() == [f"{URL} for task t1 updated from dev; waiting on its checks again"]
    assert ready.record(merging.store, "sw", "t1")["on"] == {"kind": "checks", "target": URL, "previous_head": HEAD}
    assert merging.run() == [] and len(merging.queued) == 1


def refused(action, url):
    raise SwarmError("GraphQL: Pull request is not mergeable")


def test_a_queue_refusal_is_retried_on_the_next_tick(merging):
    merging.pulls[URL] = GREEN
    assert merging.run(queuer=refused) == [f"could not queue {URL} for task t1: GraphQL: Pull request is not mergeable"]
    assert ready.record(merging.store, "sw", "t1")["on"]["kind"] == "checks"
    assert ready.record(merging.store, "sw", "t1")["refusals"] == 1
    assert merging.run() == [f"queued {URL} for task t1"]


def test_a_pull_request_the_queue_keeps_refusing_reopens_the_task(merging):
    merging.pulls[URL] = GREEN
    for _ in range(4):
        assert merging.run(queuer=refused) == [
            f"could not queue {URL} for task t1: GraphQL: Pull request is not mergeable"
        ]
    assert merging.run(queuer=refused) == [
        f"task t1 reopened, {URL} could not be queued: GraphQL: Pull request is not mergeable",
        f"removed the worktree {TOP}",
    ]
    assert merging.rows["t1"]["state"] == "open"


@pytest.mark.parametrize(
    "error",
    [
        "GitHub API: timed out",
        "error connecting to api.github.com",
        "HTTP 502",
        "the current pull request head must pass Tests before queueing",
    ],
)
def test_an_outage_or_a_hold_while_queueing_never_counts_toward_the_reopen(merging, error):
    merging.pulls[URL] = GREEN

    def down(action, url):
        raise SwarmError(error)

    for _ in range(6):
        assert merging.run(queuer=down) == [f"could not queue {URL} for task t1: {error}"]
    assert "refusals" not in ready.record(merging.store, "sw", "t1")
    assert merging.rows["t1"]["state"] == "pr"


def test_a_pull_request_into_another_base_counts_as_a_refusal(merging):
    merging.pulls[URL] = GREEN

    def elsewhere(action, url):
        raise SwarmError("swarm merge queue operations require a pull request into dev")

    merging.run(queuer=elsewhere)
    assert ready.record(merging.store, "sw", "t1")["refusals"] == 1


def test_a_pull_request_that_merges_between_the_checks_reads_waits_for_the_next_tick(merging):
    reads = iter([GREEN, MERGED])
    run = ready.Pass(
        merging.store, "sw", merging.ledger, lambda url: next(reads), merging.pulls.get, NOW, merging.queue
    )
    assert ready.merge_pass(run, merging.rows) == []
    assert merging.queued == [] and merging.rows["t1"]["state"] == "pr"
    merging.pulls[URL] = MERGED
    assert merging.run()[0] == f"task t1 done, {URL} merged"


def test_a_merged_pull_request_closes_the_task_with_the_done_proof_and_removes_the_worktree(merging):
    merging.rows["t1"]["group_members"] = ["t2", "t9"]
    merging.pulls[URL] = MERGED
    assert merging.run() == [f"task t1 done, {URL} merged", f"removed the worktree {TOP}"]
    assert merging.rows["t1"]["state"] == "done" and merging.rows["t1"]["pr_url"] == URL
    assert merging.rows["t1"]["awaiting"] == ""
    assert merging.rows["t2"]["state"] == "done" and merging.rows["t2"]["pr_url"] == URL
    assert merging.shell.calls == [REMOVE]
    assert ready.record(merging.store, "sw", "t1") is None
    assert merging.store.handoff("sw", "t1") == ""
    assert merging.run() == []


def test_a_merge_only_the_uncached_reread_sees_closes_the_task(merging):
    merging.rows["t1"]["group_members"] = ["t2"]
    queued_at_now(merging)
    run = ready.Pass(merging.store, "sw", merging.ledger, lambda url: GREEN, lambda url: MERGED, NOW + 60_000)
    assert ready.merge_pass(run, merging.rows) == [f"task t1 done, {URL} merged", f"removed the worktree {TOP}"]
    assert merging.rows["t2"]["state"] == "done" and merging.store.handoff("sw", "t1") == ""


def test_a_group_member_already_done_keeps_its_own_record(merging):
    merging.rows["t1"]["group_members"] = ["t2"]
    merging.rows["t2"].update({"state": "done", "pr_url": "https://github.com/o/r/pull/9"})
    merging.pulls[URL] = MERGED
    merging.run()
    assert merging.rows["t2"]["pr_url"] == "https://github.com/o/r/pull/9"


def test_the_merge_pass_carries_every_released_task_in_one_tick(merging):
    other = "https://github.com/o/r/pull/8"
    merging.rows.clear()
    merging.rows.update(
        {
            "t0": {"id": "t0", "state": "open", "claimed_by": ""},
            "t3": {"id": "t3", "state": "pr", "claimed_by": "", "pr_url": other, "awaiting": "merge"},
            "t1": {"id": "t1", "state": "pr", "claimed_by": AGENT, "pr_url": URL, "awaiting": "merge"},
            "t4": {"id": "t4", "state": "pr", "claimed_by": "", "pr_url": other, "awaiting": "merge"},
        }
    )
    merging.pulls[URL] = GREEN
    assert merging.run() == [
        "task t3 left the merge pass",
        f"queued {URL} for task t1",
        "task t4 left the merge pass",
    ]


def test_a_merge_seen_through_the_queue_closes_the_task(merging):
    queued_at_now(merging)
    merging.pulls[URL] = QUEUED
    merging.run(NOW + 60_000)
    merging.pulls[URL] = MERGED
    assert merging.run(NOW + 120_000)[0] == f"task t1 done, {URL} merged"


def test_a_worktree_the_merge_pass_cannot_remove_is_reported(merging):
    merging.pulls[URL] = MERGED
    merging.shell.fail = (("bash", str(stack.WT_SCRIPT), "done"),)
    assert merging.run() == [f"task t1 done, {URL} merged", f"could not remove the worktree {TOP}: boom"]
    assert merging.rows["t1"]["state"] == "done"


@pytest.mark.parametrize(
    "pull, what",
    [(RED, "has red checks"), (CLOSED, "was closed without merging")],
)
def test_red_checks_or_a_closed_pull_request_reopen_the_task_for_the_next_engineer(merging, pull, what):
    merging.pulls[URL] = pull
    assert merging.run() == [f"task t1 reopened, {URL} {what}", f"removed the worktree {TOP}"]
    assert merging.rows["t1"]["state"] == "open" and merging.rows["t1"]["claimed_by"] == ""
    assert merging.rows["t1"]["awaiting"] == ""
    assert merging.ledger.comments == [
        (
            "t1",
            f"Reopened by the swarm: its pull request {URL} {what}. The next engineer continues it from the "
            "handoff its engineer left.",
            "swarm",
        ),
    ]
    assert merging.shell.calls == [[*REMOVE, "--pushed"]]
    assert merging.store.handoff("sw", "t1") == "the handoff"
    assert merging.store.claimant("sw", "t1") is None
    assert ready.record(merging.store, "sw", "t1") is None


def test_a_pull_request_closed_while_queued_reopens_as_closed(merging):
    queued_at_now(merging)
    merging.pulls[URL] = CLOSED
    assert merging.run(NOW + 60_000)[0] == f"task t1 reopened, {URL} was closed without merging"


def test_a_reopen_leaves_a_task_the_ledger_already_moved_on(merging):
    merging.ledger.rows = {"t1": {**merging.rows["t1"], "state": "done"}}
    merging.pulls[URL] = RED
    merging.run()
    assert merging.ledger.rows["t1"]["state"] == "done" and merging.ledger.rows["t1"]["claimed_by"] == AGENT


def test_a_pull_request_that_leaves_the_queue_unmerged_reopens_the_task(merging):
    queued_at_now(merging)
    merging.pulls[URL] = QUEUED
    merging.run(NOW + 60_000)
    merging.pulls[URL] = GREEN
    assert merging.run(NOW + 120_000)[0] == f"task t1 reopened, {URL} left the merge queue without merging"
    assert merging.rows["t1"]["state"] == "open"


def test_a_fresh_queue_entry_not_yet_listed_holds_until_its_grace_runs_out(merging):
    queued_at_now(merging)
    assert merging.run(NOW + waits.FRESH_MS - 1) == []
    assert merging.rows["t1"]["state"] == "pr"
    assert merging.run(NOW + waits.FRESH_MS)[0] == f"task t1 reopened, {URL} left the merge queue without merging"


def test_a_task_a_new_engineer_took_over_leaves_the_merge_pass(merging):
    merging.rows["t1"]["claimed_by"] = "engineer@a1b2c3-0002"
    merging.pulls[URL] = GREEN
    assert merging.run() == ["task t1 left the merge pass: engineer@a1b2c3-0002 works it"]
    assert merging.rows["t1"]["awaiting"] == "" and merging.queued == []
    assert ready.record(merging.store, "sw", "t1") is None


@pytest.mark.parametrize("claimant, line", [(AGENT, f": {AGENT} works it"), ("", "")])
def test_a_task_without_its_record_leaves_the_merge_pass(merging, claimant, line):
    merging.rows["t1"]["claimed_by"] = claimant
    ready.forget(merging.store, "sw", "t1")
    merging.pulls[URL] = GREEN
    assert merging.run() == [f"task t1 left the merge pass{line}"]
    assert merging.rows["t1"]["awaiting"] == "" and merging.queued == []


def test_a_github_outage_holds_the_pass(merging):
    merging.pulls[URL] = None
    assert merging.run() == []
    assert ready.record(merging.store, "sw", "t1")["on"]["kind"] == "checks"


def test_awaiting_names_only_a_pull_request_task_released_to_the_tick():
    assert ready.awaiting({"state": "pr", "awaiting": "merge"})
    assert not ready.awaiting({"state": "pr", "awaiting": "approval"})
    assert not ready.awaiting({"state": "claimed", "awaiting": "merge"})
    assert not ready.awaiting({"state": "pr"})


def test_the_tick_runs_the_merge_pass_after_the_wait_pass(env, monkeypatch):
    store, ledger, rt = env
    cli.main(["sw", "create", "--repo", "/repo"])
    order = []
    monkeypatch.setattr(waits, "end_pass", lambda *args: order.append("waits") or [])
    monkeypatch.setattr(ready, "merge_pass", lambda run, rows: order.append(("merge", run.slug)) or ["merged"])
    from tests.swarm.test_delivery import FakeHerdr

    actions = cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert order == ["waits", ("merge", "sw")] and "merged" in actions


def test_the_ready_usage_names_its_handoff_document(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["sw", "ready", "--help"])
    assert "doc" in capsys.readouterr().out
