import json
import subprocess

import pytest

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


class Shell:
    def __init__(self):
        self.calls, self.status, self.head, self.fail = [], "", HEAD, ()

    def __call__(self, argv):
        self.calls.append(argv)
        out = {
            ("git", "rev-parse", "--show-toplevel"): TOP,
            ("git", "rev-parse", "--path-format=absolute"): "/home/me/dev/repo/.git",
            ("bash", str(stack.WT_SCRIPT), "root"): ROOT,
            ("git", "status", "--porcelain"): self.status,
            ("git", "rev-parse", "HEAD"): self.head,
        }.get(tuple(argv[:3]), "")
        code = 1 if tuple(argv[:3]) in self.fail else 0
        return subprocess.CompletedProcess(argv, code, stdout=out + "\n", stderr=" boom\n" if code else "")


@pytest.fixture
def released(env, monkeypatch, tmp_path):
    store, ledger, rt = env
    cli.main(["sw", "create", "--repo", "/repo"])
    cli.main(["sw", "start"])
    ledger.rows["t1"].update({"state": "pr", "pr_url": URL, "branch": "eng-t1"})
    ledger.pulls[URL] = RUNNING
    shell = Shell()
    monkeypatch.setattr(stack, "shell", shell)
    monkeypatch.setattr(cli, "now_ms", lambda: NOW)
    doc = tmp_path / "handoff.md"
    doc.write_text(HANDOFF.format(intent="Finish the parser task for the swarm."))
    return store, ledger, shell, doc


def release(doc):
    return cli.main(["sw", "--as", AGENT, "ready", str(doc)])


def test_ready_records_the_pull_request_and_head_and_ends_the_agent(released, capsys):
    store, ledger, _, doc = released
    capsys.readouterr()
    assert release(doc) == 0
    assert ledger.rows["t1"]["state"] == "pr" and ledger.rows["t1"]["awaiting"] == ready.AWAITING
    assert ready.record(store, "sw", "t1") == {
        "url": URL,
        "head": HEAD,
        "worktree": TOP,
        "agent": AGENT,
        "at": NOW,
        "on": {"kind": "checks", "target": URL, "head": HEAD},
    }
    assert store.handoff("sw", "t1") == doc.read_text()
    assert store.handoff_envelope("sw", "t1")["reason"] == "exit"
    assert [a.state for a in store.agents("sw") if a.name == AGENT] == ["finished"]
    assert json.loads(capsys.readouterr().out) == {
        "task": "t1",
        "pr_url": URL,
        "head": HEAD,
        "awaiting": ready.AWAITING,
        "next": "stop now; the swarm carries the pull request to merge and closes this session",
    }


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
    assert [a.state for a in store.agents("sw") if a.name == AGENT] == ["working"]


@pytest.mark.parametrize(
    "pull, says",
    [
        (None, f"cannot read pull request {URL} from GitHub; run swarm ready again when it answers"),
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
    assert ready.record(store, "sw", "t1") is None
    assert [a.state for a in store.agents("sw") if a.name == AGENT] == ["working"]


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
    ready.save(store, "sw", "t1", ready.held_record(URL, HEAD, TOP, AGENT, NOW))
    pulls, queued, shell = {URL: RUNNING}, [], Shell()
    monkeypatch.setattr(stack, "shell", shell)

    def queue(action, url):
        queued.append((action, url))
        return {"url": url, "queued": True, **getattr(queue, "extra", {})}

    def run(now=NOW):
        return ready.merge_pass(store, "sw", rows, ledger, pulls.get, pulls.get, now, queue)

    rig = type("Rig", (), {})()
    rig.store, rig.rows, rig.ledger, rig.pulls, rig.queued, rig.shell, rig.run, rig.queue = (
        store,
        rows,
        ledger,
        pulls,
        queued,
        shell,
        run,
        queue,
    )
    return rig


def test_the_merge_pass_waits_while_checks_run(merging):
    assert merging.run() == []
    assert merging.queued == [] and merging.rows["t1"]["state"] == "pr"


def test_the_merge_pass_queues_on_green_checks_and_watches_the_queue(merging):
    merging.pulls[URL] = GREEN
    assert merging.run() == [f"queued {URL} for task t1"]
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


def test_a_queue_refusal_is_retried_on_the_next_tick(merging):
    merging.pulls[URL] = GREEN

    def refused(action, url):
        raise SwarmError("GitHub API failed")

    assert ready.merge_pass(
        merging.store, "sw", merging.rows, merging.ledger, merging.pulls.get, merging.pulls.get, NOW, refused
    ) == [f"could not queue {URL} for task t1: GitHub API failed"]
    assert ready.record(merging.store, "sw", "t1")["on"]["kind"] == "checks"
    assert merging.run() == [f"queued {URL} for task t1"]


def test_a_merged_pull_request_closes_the_task_with_the_done_proof_and_removes_the_worktree(merging):
    merging.rows["t1"]["group_members"] = ["t2"]
    merging.pulls[URL] = MERGED
    assert merging.run() == [f"task t1 done, {URL} merged", f"removed the worktree {TOP}"]
    assert merging.rows["t1"]["state"] == "done" and merging.rows["t1"]["pr_url"] == URL
    assert merging.rows["t1"]["awaiting"] == ""
    assert merging.rows["t2"]["state"] == "done" and merging.rows["t2"]["pr_url"] == URL
    assert merging.shell.calls == [["bash", str(stack.WT_SCRIPT), "done", "engineer-a1b2c3-0001", "--repo", TOP]]
    assert ready.record(merging.store, "sw", "t1") is None
    assert merging.store.handoff("sw", "t1") == ""
    assert merging.run() == []


def test_a_merge_seen_through_the_queue_closes_the_task(merging):
    merging.pulls[URL] = GREEN
    merging.run()
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
        ("t1", ready.REOPENED.format(url=URL, what=what), "swarm"),
    ]
    assert merging.shell.calls == [
        ["bash", str(stack.WT_SCRIPT), "done", "engineer-a1b2c3-0001", "--repo", TOP, "--pushed"]
    ]
    assert merging.store.handoff("sw", "t1") == "the handoff"
    assert ready.record(merging.store, "sw", "t1") is None


def test_a_pull_request_that_leaves_the_queue_unmerged_reopens_the_task(merging):
    merging.pulls[URL] = GREEN
    merging.run()
    merging.pulls[URL] = QUEUED
    merging.run(NOW + 60_000)
    merging.pulls[URL] = GREEN
    assert merging.run(NOW + 120_000)[0] == f"task t1 reopened, {URL} left the merge queue without merging"
    assert merging.rows["t1"]["state"] == "open"


def test_a_task_a_new_engineer_took_over_leaves_the_merge_pass(merging):
    merging.rows["t1"]["claimed_by"] = "engineer@a1b2c3-0002"
    merging.pulls[URL] = GREEN
    assert merging.run() == ["task t1 left the merge pass: engineer@a1b2c3-0002 works it"]
    assert merging.rows["t1"]["awaiting"] == "" and merging.queued == []
    assert ready.record(merging.store, "sw", "t1") is None


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
    monkeypatch.setattr(ready, "merge_pass", lambda *args: order.append(("merge", args[1])) or ["merged"])
    from tests.swarm.test_delivery import FakeHerdr

    actions = cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert order == ["waits", ("merge", "sw")] and "merged" in actions


def test_the_ready_usage_names_its_handoff_document(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["sw", "ready", "--help"])
    assert "doc" in capsys.readouterr().out
