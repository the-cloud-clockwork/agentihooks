import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts.gates import Call, Who
from scripts.gates.base import Decision
from scripts.gates.claim_stop import ClaimStop
from scripts.gates.verdicts import Verdicts
from scripts.swarm import capacity, cli, linked_issues, merge_queue, ready, stack, tick, waits
from scripts.swarm.ledger_events import PullRequest
from scripts.swarm.ledger_events import view as read_pull
from scripts.swarm.store import RedisStore, SwarmError
from tests.swarm.test_cli import HANDOFF, env

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
BODY = ["gh", "pr", "view", URL, "--json", "body", "--jq", ".body"]


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
    ledger.update_task = lambda slug, task, fields, by="swarm", if_state=(), if_match=None: (
        ledger.updates.append((task, fields, by)) or update(slug, task, fields, by, if_state, if_match)
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


@pytest.fixture
def primary_checkout(released, monkeypatch):
    shell = released[2]

    def primary(argv):
        if argv == ["git", "rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(argv, 0, stdout="/home/me/dev/repo\n", stderr="")
        return shell(argv)

    monkeypatch.setattr(stack, "shell", primary)
    return released


@pytest.mark.parametrize("kind", ["code", "ci", "tune", "troubleshoot", "ops"])
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


@pytest.mark.parametrize("kind", ["task", "merge"])
def test_release_keeps_an_open_dependency_wait_after_the_agent_leaves(released, monkeypatch, kind):
    from scripts.swarm import idle, stall_watch
    from tests.swarm.test_delivery import FakeHerdr

    store, ledger, _, doc = released
    target = "t2" if kind == "task" else URL + "0"
    on = {"kind": kind, "target": target}
    ledger.rows["t2"]["state"] = "pr"
    ledger.task = lambda slug, task_id: ledger.rows.get(task_id) if slug == "sw" else None
    ledger.pulls[URL], ledger.pulls[URL + "0"] = RED, RED
    idle.declare_wait(store.redis, "sw", AGENT, NOW + 60_000, "shared repair", NOW, on=on)
    assert release(doc) == 0
    assert ready.record(store, "sw", "t1")["dependency"] == on
    monkeypatch.setattr(cli.ledger_events, "views", lambda urls, cache=None: {url: ledger.pulls[url] for url in urls})
    monkeypatch.setattr(stall_watch, "ntfy", lambda *args: "")
    for minutes in (0, 10, 30, 60):
        monkeypatch.setattr(cli, "now_ms", lambda: NOW + minutes * 60_000)
        cli.run_tick(store, "sw", ledger, cli.HerdrRuntime(), FakeHerdr({}))
        assert ledger.rows["t1"]["state"] == "pr"
        assert ledger.rows["t1"]["awaiting"] == ready.AWAITING
        assert not any(a.task == "t1" for a in store.agents("sw"))
        assert ready.record(store, "sw", "t1")["dependency"] == on
    if kind == "task":
        ledger.rows["t2"]["state"] = "done"
    else:
        ledger.pulls[target] = MERGED
    run = ready.Pass(store, "sw", ledger, ledger.pulls.get, ledger.pulls.get, NOW + 61 * 60_000)
    ready.merge_pass(run, ledger.rows)
    assert ledger.rows["t1"]["state"] == "open"
    assert ledger.rows["t1"]["awaiting"] == ready.FIX


@pytest.mark.parametrize(
    "on, until",
    [
        ({"kind": "task", "target": "t2"}, NOW),
        ({"kind": "task", "target": "t1"}, NOW + 60_000),
        ({"kind": "merge", "target": URL}, NOW + 60_000),
        ({"kind": "reply", "target": "review"}, NOW + 60_000),
        ({}, NOW + 60_000),
    ],
)
def test_release_does_not_park_on_an_expired_or_unrelated_wait(released, on, until):
    from scripts.swarm import idle

    store, ledger, _, doc = released
    ledger.pulls[URL] = RED
    idle.declare_wait(store.redis, "sw", AGENT, until, "wait", NOW - 60_000, on=on)
    assert release(doc) == 0
    assert "dependency" not in ready.record(store, "sw", "t1")
    run = ready.Pass(store, "sw", ledger, ledger.pulls.get, ledger.pulls.get, NOW)
    ready.merge_pass(run, ledger.rows)
    assert ledger.rows["t1"]["state"] == "open"


@pytest.mark.parametrize("pull", [MERGED, CLOSED])
def test_a_finished_own_pull_request_is_handled_even_with_an_open_dependency(merging, pull):
    on = {"kind": "task", "target": "t2"}
    found = ready.record(merging.store, "sw", "t1")
    found["dependency"] = on
    ready.save(merging.store, "sw", "t1", found)
    merging.pulls[URL] = pull
    merging.run()
    assert merging.rows["t1"]["state"] == ("done" if pull is MERGED else "open")


@pytest.mark.parametrize("retained", [TOP, "", None])
@pytest.mark.parametrize("source", ["ready", "handoff"])
def test_ready_from_primary_carries_an_unchanged_queued_head(primary_checkout, retained, source):
    store, ledger, shell, doc = primary_checkout
    ledger.pulls[URL] = QUEUED
    if source == "ready":
        previous = ready.held_record(URL, HEAD, retained, "predecessor", 1)
        if retained is None:
            previous.pop("worktree")
        ready.save(store, "sw", "t1", previous)
    else:
        envelope = {"pull_request": {"url": URL}, "remote_head": HEAD}
        if retained is not None:
            envelope["worktree"] = retained or "unknown"
        store.put_handoff("sw", "t1", doc.read_text(), envelope=envelope)
    assert release(doc) == 0
    found = ready.record(store, "sw", "t1")
    assert found["worktree"] == (retained or "")
    assert found["head"] == HEAD
    assert found["agent"] == AGENT
    assert found["on"] == {"kind": "merge", "target": URL}
    assert found["queued_at"] == NOW
    assert not working(store)
    assert ledger.rows["t1"]["awaiting"] == "merge"


@pytest.mark.parametrize(
    ("source", "url", "head"),
    [
        ("ready", URL, "b" * 40),
        ("handoff", URL, "b" * 40),
        ("ready", URL + "0", HEAD),
        ("handoff", URL + "0", HEAD),
        ("ready", URL, None),
        ("handoff", URL, None),
        ("missing", URL, HEAD),
        ("unknown_pull", URL, HEAD),
    ],
)
def test_ready_from_primary_refuses_a_moved_queued_head(primary_checkout, capsys, source, url, head):
    store, ledger, shell, doc = primary_checkout
    ledger.pulls[URL] = QUEUED
    previous = None
    if source == "ready":
        previous = ready.held_record(url, head, TOP, "predecessor", 1)
        ready.save(store, "sw", "t1", previous)
    elif source == "handoff":
        store.put_handoff(
            "sw",
            "t1",
            doc.read_text(),
            envelope={"pull_request": {"url": url}, "remote_head": head, "worktree": TOP},
        )
    elif source == "unknown_pull":
        store.put_handoff("sw", "t1", doc.read_text(), envelope={"pull_request": "unknown"})
    capsys.readouterr()
    assert release(doc) == 1
    assert capsys.readouterr().err == (
        "swarm: park and ready remove only a worktree wt.sh new made under "
        "/home/me/dev/worktrees; /home/me/dev/repo is not one\n"
    )
    assert working(store)
    assert ready.record(store, "sw", "t1") == previous


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


@pytest.mark.parametrize("pull", [GREEN, RUNNING, QUEUED])
def test_repeated_ticks_never_respawn_a_released_task_waiting_on_its_pull_request(released, monkeypatch, pull):
    from scripts.swarm import stall_watch
    from tests.swarm.test_delivery import FakeHerdr

    store, ledger, _, doc = released
    assert release(doc) == 0
    ledger.pulls[URL] = pull
    monkeypatch.setattr(cli.ledger_events, "views", lambda urls, cache=None: {url: ledger.pulls[url] for url in urls})
    monkeypatch.setattr(stall_watch, "ntfy", lambda *args: "")
    attempts = []

    def queue(action, url, journal=None):
        attempts.append(url)
        if pull.queued:
            return {}
        raise SwarmError("the current pull request head must pass Tests")

    merge_pass = ready.Pass
    monkeypatch.setattr(ready, "Pass", lambda *args: merge_pass(*args, queue=queue))
    for minutes in (0, 10, 30, 60):
        monkeypatch.setattr(cli, "now_ms", lambda: NOW + minutes * 60_000)
        cli.run_tick(store, "sw", ledger, cli.HerdrRuntime(), FakeHerdr({}))
        assert ledger.rows["t1"]["state"] == "pr"
        assert ledger.rows["t1"]["awaiting"] == ready.AWAITING
        assert not any(a.task == "t1" for a in store.agents("sw"))
        assert ready.record(store, "sw", "t1")["head"] == HEAD
    assert attempts == ([URL] * 4 if pull is GREEN else [URL] if pull is QUEUED else [])


@pytest.mark.parametrize("trigger", ["queue_exit", "ready_mark"])
def test_reopening_keeps_a_live_claimant_instead_of_spawning_a_successor(released, monkeypatch, trigger):
    from scripts.swarm.ledger_events import pull_request

    store, ledger, _, doc = released
    runtime = cli.HerdrRuntime()
    runtime.stuck.add(AGENT)
    monkeypatch.setattr("scripts.swarm.runtime.HerdrRuntime", lambda: runtime)
    if trigger == "ready_mark":
        assert release(doc) == 0
        pull = RED
    else:
        pull = pull_request({"state": "OPEN", "mergeQueueEntry": {"id": "entry", "state": "UNMERGEABLE"}})
    attempts = list(runtime.spawned)
    rows = ledger.rows
    run = ready.Pass(
        store, "sw", ledger, lambda url: pull, lambda url: pull, NOW, lambda *args, **kwargs: {"queued": False}
    )
    ready.merge_pass(run, rows)
    tick.tick("sw", store, ledger, runtime, NOW + 1_000)
    assert runtime.spawned == attempts
    assert store.claimant("sw", "t1") == AGENT
    assert rows["t1"]["claimed_by"] == AGENT
    assert AGENT in runtime.live_names()


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
        ({"pr_url": "", "kind": "tune"}, "record the pull request with swarm pr before swarm ready"),
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
        self.rows, self.comments, self.alerts = rows, [], []

    def raise_alert(self, slug, source, text):
        assert slug == "sw"
        self.alerts.append((source, text))

    def update_task(self, slug, task_id, fields, by="swarm", if_state=(), if_match=None):
        from tests.swarm.test_tick import check_update_guards

        check_update_guards(if_state, if_match)
        assert slug == "sw" and by == "swarm"
        row = self.rows[task_id]
        if any(
            row.get(key, 0 if key == "claim_generation" else "") != value for key, value in (if_match or {}).items()
        ):
            return dict(row)
        if not if_state or row["state"] in if_state:
            if "claimed_by" in fields and fields["claimed_by"] != row.get("claimed_by"):
                row["claim_generation"] = row.get("claim_generation", 0) + 1
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
    monkeypatch.setattr("scripts.swarm.runtime.HerdrRuntime.live_names", lambda self: set())
    monkeypatch.setattr(stack, "shell", shell)

    def queue(action, url, journal=None):
        queued.append((action, url))
        queue.journals.append(journal)
        return {"url": url, "queued": True, **getattr(queue, "extra", {})}

    queue.journals = []

    def exits(url, since):
        exits.calls.append((url, since))
        if isinstance(exits.failures, Exception):
            raise exits.failures
        return exits.failures

    exits.calls, exits.failures = [], [f"Tests {URL}/runs/1"]

    def run(now=NOW, queuer=queue):
        return ready.merge_pass(ready.Pass(store, "sw", ledger, pulls.get, pulls.get, now, queuer, exits), rows)

    return SimpleNamespace(
        store=store,
        rows=rows,
        ledger=ledger,
        pulls=pulls,
        queued=queued,
        shell=shell,
        run=run,
        queue=queue,
        exits=exits,
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


def test_a_queue_pass_watches_the_merge_whatever_the_queue_command_reports(merging):
    merging.queue.extra = {"queued": False, "entry": None}
    queued_at_now(merging)
    assert merging.queued == [("queue", URL)]
    assert ready.record(merging.store, "sw", "t1")["on"] == {"kind": "merge", "target": URL}


def test_the_merge_pass_squash_merges_without_a_queue_or_tests_workflow(merging, monkeypatch):
    merging.pulls[URL] = GREEN
    monkeypatch.setattr(merge_queue.ledger_events, "view", read_pull)
    pull = {
        "id": "PR_one",
        "state": "OPEN",
        "headRefOid": HEAD,
        "baseRefName": "dev",
        "baseRef": {"branchProtectionRule": None},
        "mergeQueueEntry": None,
        "isDraft": False,
        "reviewDecision": None,
        "statusCheckRollup": {"state": "SUCCESS"},
        "repository": {"mergeQueue": None, "nameWithOwner": "o/r"},
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "id": HEAD,
                        "statusCheckRollup": {"contexts": {"nodes": []}},
                        "checkSuites": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                    }
                }
            ]
        },
    }
    merged_heads = []

    def github(command, **kwargs):
        if command[2] == "repos/o/r/rules/branches/dev":
            return subprocess.CompletedProcess(command, 0, "[]")
        if command[:3] != ["gh", "api", "graphql"]:
            return subprocess.CompletedProcess(command, 1, "", "gh: Not Found (HTTP 404)")
        if command[4] == f"query={merge_queue.MERGE}":
            merged_heads.append(command[-1])
            pull["state"] = "MERGED"
            merging.pulls[URL] = MERGED
            data = {"mergePullRequest": {"pullRequest": {"state": "MERGED"}}}
        else:
            data = {"resource": pull}
        return subprocess.CompletedProcess(command, 0, json.dumps({"data": data}))

    def queue(action, url, journal=None):
        return merge_queue.operate(action, url, github, journal=journal)

    merging.run(queuer=queue)
    assert merged_heads == [f"head={HEAD}"]
    assert merging.run() == [f"task t1 done, {URL} merged", f"removed the worktree {TOP}"]
    assert merging.rows["t1"]["state"] == "done" and merging.rows["t1"]["awaiting"] == ""
    assert ready.record(merging.store, "sw", "t1") is None


def refused(action, url, journal=None):
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
    ]
    assert merging.rows["t1"]["state"] == "open" and merging.rows["t1"]["awaiting"] == "fix"
    assert merging.ledger.comments == [
        (
            "t1",
            f"Reopened by the swarm: its pull request {URL} could not be queued: GraphQL: Pull request is not "
            "mergeable. The next engineer continues it from the handoff its engineer left.",
            "swarm",
        ),
    ]


@pytest.mark.parametrize(
    "error",
    [
        "gh: Not Found (HTTP 404)",
        "cannot establish the base of the green checks",
        "squash merge requires a ready pull request with green checks and no pending review",
    ],
)
def test_an_unknown_merge_error_reopens_the_task_with_its_reason(merging, error):
    merging.pulls[URL] = GREEN

    def failed(action, url, journal=None):
        raise SwarmError(error)

    assert merging.run(queuer=failed) == [
        f"task t1 reopened, {URL} could not be queued: {error}",
    ]
    assert merging.rows["t1"]["state"] == "open"
    assert merging.rows["t1"]["claimed_by"] == "" and merging.rows["t1"]["awaiting"] == "fix"
    assert error in merging.ledger.comments[0][1]
    assert merging.store.handoff("sw", "t1") == "the handoff"
    assert merging.store.claimant("sw", "t1") is None
    assert ready.record(merging.store, "sw", "t1") is None


@pytest.mark.parametrize(
    "error",
    [
        "GitHub API: timed out",
        "GitHub API: Command gh timed out after 20 seconds",
        "error connecting to api.github.com",
        "HTTP 502",
        "gh: Service Unavailable (HTTP 503)",
        "the current pull request head must pass Tests before queueing",
    ],
)
def test_an_outage_or_a_hold_while_queueing_never_counts_toward_the_reopen(merging, error):
    merging.pulls[URL] = GREEN

    def down(action, url, journal=None):
        raise SwarmError(error)

    for _ in range(6):
        assert merging.run(queuer=down) == [f"could not queue {URL} for task t1: {error}"]
    assert "refusals" not in ready.record(merging.store, "sw", "t1")
    assert merging.rows["t1"]["state"] == "pr"


def test_a_pull_request_into_another_base_counts_as_a_refusal(merging):
    merging.pulls[URL] = GREEN

    def elsewhere(action, url, journal=None):
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
    assert merging.shell.calls == [BODY, REMOVE]
    assert ready.record(merging.store, "sw", "t1") is None
    assert merging.store.handoff("sw", "t1") == ""
    assert merging.run() == []


@pytest.mark.parametrize("kind", ["tune", "troubleshoot"])
def test_a_merged_pull_request_leaves_a_task_of_another_kind_open_for_its_proof(merging, kind):
    merging.rows["t1"]["kind"] = kind
    merging.pulls[URL] = MERGED
    assert merging.run() == [f"task t1 awaits its proof, {URL} merged", f"removed the worktree {TOP}"]
    row = merging.rows["t1"]
    assert row["state"] == "open" and row["claimed_by"] == "" and row["awaiting"] == ""
    assert row["pr_url"] == URL and row["claim_generation"] == 1
    assert merging.ledger.comments == [("t1", ready.PROOF.format(url=URL, kind=kind), "swarm")]
    assert merging.store.handoff("sw", "t1") == "the handoff"
    assert ready.record(merging.store, "sw", "t1") is None
    assert merging.store.redis.get(merging.store.key("sw", "claim", "t1")) is None
    assert merging.shell.calls == [REMOVE]
    assert merging.run() == []


def test_a_merged_code_task_waits_for_declared_post_merge_proof(merging):
    merging.rows["t1"]["contract"] = {"after": "yes"}
    merging.pulls[URL] = MERGED
    assert merging.run() == [f"task t1 awaits its proof, {URL} merged", f"removed the worktree {TOP}"]
    row = merging.rows["t1"]
    assert row["state"] == "open" and row["awaiting"] == "verify" and row["claimed_by"] == ""
    assert row["pr_url"] == URL
    assert merging.store.handoff("sw", "t1") == "the handoff"
    assert ready.record(merging.store, "sw", "t1") is None
    assert merging.store.redis.get(merging.store.key("sw", "claim", "t1")) is None
    assert merging.run() == []


@pytest.mark.parametrize("after_at,until", [("", NOW + 60_000), (str(NOW + 120_000), NOW + 120_000), (str(NOW), None)])
def test_live_post_merge_proof_owner_keeps_claim_with_declared_wait(merging, monkeypatch, after_at, until):
    from scripts.swarm import idle

    merging.rows["t1"]["contract"] = {"after": "yes", "after_at": after_at}
    merging.pulls[URL] = MERGED
    monkeypatch.setattr(ready, "_owners", lambda run, task_id: [SimpleNamespace(name=AGENT)])
    assert merging.run() == []
    assert merging.rows["t1"]["state"] == "pr"
    assert merging.rows["t1"]["claimed_by"] == AGENT
    assert merging.shell.calls == []
    entry = idle.wait(merging.store.redis, "sw", AGENT)
    assert (entry["until"] if entry else None) == until
    if entry:
        assert entry["reason"] == "post merge proof" and entry["at"] == NOW


@pytest.mark.parametrize(
    "contract,proof", [({"after": "no"}, {}), ({"after": "yes"}, {"command": "probe", "output": "ok"})]
)
def test_merged_code_with_no_missing_after_proof_still_closes(merging, contract, proof):
    merging.rows["t1"].update(contract=contract, proof=proof)
    merging.pulls[URL] = MERGED
    assert merging.run()[0] == f"task t1 done, {URL} merged"
    assert merging.rows["t1"]["state"] == "done"


def test_post_merge_verification_waits_for_maturity_or_master_reopen(monkeypatch):
    from scripts.swarm import claim_hold

    monkeypatch.setattr(claim_hold, "time", lambda: 10)
    task = {"awaiting": "verify", "contract": {"after": "yes", "after_at": "10001"}}
    assert claim_hold.hold(task, {}, {}, {}, "") == "it waits for its post merge proof window or a master reopen"
    task["contract"].pop("after_at")
    assert claim_hold.hold(task, {}, {}, {}, "")
    task["contract"]["after_at"] = ""
    assert claim_hold.hold(task, {}, {}, {}, "")
    task["contract"]["after_at"] = "10000"
    assert claim_hold.verification(task) == ""
    task["contract"]["after_at"] = "9999"
    assert claim_hold.verification(task) == ""
    task["awaiting"] = ""
    task["contract"]["after_at"] = "10001"
    assert claim_hold.verification(task) == ""


def test_a_merged_pull_request_of_another_kind_waits_while_its_owner_is_live(merging, monkeypatch):
    merging.rows["t1"]["kind"] = "tune"
    merging.pulls[URL] = MERGED
    monkeypatch.setattr(ready, "_owners", lambda run, task_id: [AGENT])
    assert merging.run() == []
    assert merging.rows["t1"]["state"] == "pr" and merging.shell.calls == []
    assert ready.record(merging.store, "sw", "t1") is not None


def test_a_merged_pull_request_of_another_kind_keeps_its_record_when_the_claim_release_is_refused(merging):
    merging.rows["t1"]["kind"] = "tune"
    merging.pulls[URL] = MERGED
    merging.store.redis.set(merging.store.key("sw", "claim", "t1"), "engineer@other-0002")
    assert merging.run() == []
    assert merging.ledger.comments == [] and merging.shell.calls == []
    assert ready.record(merging.store, "sw", "t1") is not None


ISSUE = "https://github.com/o/r/issues/5"
NAMED = "https://github.com/o/r/issues/12"
MEMBER = "https://github.com/o/r/issues/7"
CLOSED_BY = f"Closed by the swarm: pull request {URL} merged into dev."


@pytest.fixture
def issues(merging, monkeypatch):
    states, body = {ISSUE: "OPEN", NAMED: "OPEN"}, "Fixes the parser.\n\nCloses #12\n"

    def shell(argv):
        code = 1 if tuple(argv[:3]) in merging.shell.fail else 0
        if argv[:3] == ["gh", "pr", "view"]:
            assert argv == BODY
            merging.shell.calls.append(argv)
            return subprocess.CompletedProcess(argv, code, stdout=body, stderr=" boom\n")
        if argv[:3] == ["gh", "issue", "view"]:
            assert argv[4:] == ["--json", "state", "--jq", ".state"]
            merging.shell.calls.append(argv)
            return subprocess.CompletedProcess(argv, code, stdout=states[argv[3]] + "\n", stderr=" boom\n")
        return merging.shell(argv)

    monkeypatch.setattr(stack, "shell", shell)
    merging.rows["t1"]["issue_url"] = ISSUE
    merging.pulls[URL] = MERGED
    return states


def issue_writes(merging):
    return [c for c in merging.shell.calls if c[:3] in (["gh", "issue", "comment"], ["gh", "issue", "close"])]


def test_a_merged_ready_pull_request_closes_the_task_issue_and_the_issue_it_names(merging, issues):
    assert merging.run() == [
        f"task t1 done, {URL} merged",
        f"closed issue {ISSUE}",
        f"closed issue {NAMED}",
        f"removed the worktree {TOP}",
    ]
    assert issue_writes(merging) == [
        ["gh", "issue", "close", ISSUE],
        ["gh", "issue", "comment", ISSUE, "--body", CLOSED_BY],
        ["gh", "issue", "close", NAMED],
        ["gh", "issue", "comment", NAMED, "--body", CLOSED_BY],
    ]


def test_an_issue_linked_to_a_task_still_open_stays_open(merging, issues):
    merging.rows["t2"]["issue_url"] = NAMED
    merging.rows["t3"] = {"id": "t3", "state": "done", "issue_url": ISSUE}
    merging.run()
    assert issue_writes(merging) == [
        ["gh", "issue", "close", ISSUE],
        ["gh", "issue", "comment", ISSUE, "--body", CLOSED_BY],
    ]


def test_an_issue_already_closed_gets_no_second_comment(merging, issues):
    issues[ISSUE] = "CLOSED"
    assert f"closed issue {ISSUE}" not in merging.run()
    assert issue_writes(merging) == [
        ["gh", "issue", "close", NAMED],
        ["gh", "issue", "comment", NAMED, "--body", CLOSED_BY],
    ]


def test_an_issue_the_swarm_cannot_close_is_reported_and_the_task_still_closes(merging, issues):
    merging.shell.fail = (("gh", "issue", "close"),)
    actions = merging.run()
    assert f"could not close issue {ISSUE}: boom" in actions
    assert merging.rows["t1"]["state"] == "done"
    assert ["gh", "issue", "comment", ISSUE, "--body", CLOSED_BY] not in merging.shell.calls


def test_a_closed_issue_whose_comment_fails_is_reported(merging, issues):
    merging.shell.fail = (("gh", "issue", "comment"),)
    actions = merging.run()
    assert f"closed issue {ISSUE}; could not comment on it: boom" in actions
    assert f"closed issue {ISSUE}" not in actions


def test_an_issue_whose_state_cannot_be_read_is_reported_without_a_comment(merging, issues):
    merging.shell.fail = (("gh", "issue", "view"),)
    actions = merging.run()
    assert f"could not close issue {ISSUE}: boom" in actions and f"could not close issue {NAMED}: boom" in actions
    assert issue_writes(merging) == []


def test_a_pull_request_body_that_cannot_be_read_still_closes_the_task_issue(merging, issues):
    merging.shell.fail = (("gh", "pr", "view"),)
    assert f"could not read the issues named by {URL}: boom" in merging.run()
    assert issue_writes(merging) == [
        ["gh", "issue", "close", ISSUE],
        ["gh", "issue", "comment", ISSUE, "--body", CLOSED_BY],
    ]


def test_a_group_member_issue_closes_with_the_group(merging, issues):
    del merging.rows["t1"]["issue_url"]
    merging.rows["t1"]["group_members"] = ["t2"]
    merging.rows["t2"]["issue_url"] = MEMBER
    issues[MEMBER] = "OPEN"
    merging.run()
    assert issue_writes(merging) == [
        ["gh", "issue", "close", MEMBER],
        ["gh", "issue", "comment", MEMBER, "--body", CLOSED_BY],
        ["gh", "issue", "close", NAMED],
        ["gh", "issue", "comment", NAMED, "--body", CLOSED_BY],
    ]


@pytest.mark.parametrize(
    ("body", "numbers"),
    [
        ("Closes #1, fixes #2 and Resolved: #3", ["1", "2", "3"]),
        ("close #4 fixed #5 resolves #6 fix #7 closed #8 resolve #9", ["4", "5", "6", "7", "8", "9"]),
        ("Refs #10, see #11, enclosed #12, prefixes #13", []),
    ],
)
def test_only_closing_keywords_name_an_issue(body, numbers):
    assert linked_issues.KEYWORD.findall(body) == numbers


@pytest.mark.parametrize("remaining", [RUNNING, CLOSED, None])
def test_a_split_task_stays_in_pr_until_its_last_pull_request_merges(merging, remaining):
    second = "https://github.com/o/r/pull/8"
    merging.rows["t1"].update(split=["1-2", "3-4"], split_pulls=[URL, second], pr_url=second)
    merging.pulls.update({URL: MERGED, second: remaining})
    assert merging.run() == []
    assert merging.rows["t1"]["state"] == "pr"
    assert merging.rows["t1"]["pr_url"] == second
    assert merging.rows["t1"]["awaiting"] == "merge"
    assert merging.shell.calls == []
    assert ready.record(merging.store, "sw", "t1") is not None
    assert merging.store.handoff("sw", "t1") == "the handoff"

    merging.pulls[second] = MERGED
    merging.run()
    assert merging.rows["t1"]["state"] == "done"
    assert merging.rows["t1"]["split_pulls"] == [URL, second]
    assert merging.rows["t1"]["awaiting"] == ""
    assert merging.shell.calls == [BODY, REMOVE]
    assert ready.record(merging.store, "sw", "t1") is None
    assert merging.store.handoff("sw", "t1") == ""
    assert merging.run() == []


def test_a_split_task_waits_for_a_pull_request_for_every_declared_half(merging):
    merging.rows["t1"].update(split=["1-2", "3-4"], split_pulls=[URL])
    merging.pulls[URL] = MERGED
    assert merging.run() == []
    assert merging.rows["t1"]["state"] == "pr"
    assert merging.rows["t1"]["awaiting"] == "merge"
    assert merging.shell.calls == []
    assert ready.record(merging.store, "sw", "t1") is not None
    assert merging.store.handoff("sw", "t1") == "the handoff"


def test_the_watched_pull_request_completes_the_remaining_split_binding(merging):
    second = "https://github.com/o/r/pull/8"
    merging.rows["t1"].update(split=["1-2", "3-4"], split_pulls=[second], pr_url=second)
    merging.pulls.update({URL: MERGED, second: MERGED})
    merging.run()
    assert merging.rows["t1"]["state"] == "done"
    assert merging.rows["t1"]["awaiting"] == ""
    assert merging.shell.calls == [BODY, REMOVE]
    assert ready.record(merging.store, "sw", "t1") is None
    assert merging.store.handoff("sw", "t1") == ""


def test_a_merged_pull_request_without_a_checkout_closes_without_removing_one(merging):
    found = ready.record(merging.store, "sw", "t1")
    found["worktree"] = ""
    ready.save(merging.store, "sw", "t1", found)
    merging.pulls[URL] = MERGED
    assert merging.run() == [f"task t1 done, {URL} merged"]
    assert merging.rows["t1"]["state"] == "done"
    assert merging.shell.calls == [BODY]
    assert ready.record(merging.store, "sw", "t1") is None


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
    assert merging.run() == [f"task t1 reopened, {URL} {what}"]
    assert merging.rows["t1"]["state"] == "open" and merging.rows["t1"]["claimed_by"] == ""
    assert merging.rows["t1"]["awaiting"] == "fix"
    assert merging.ledger.comments == [
        (
            "t1",
            f"Reopened by the swarm: its pull request {URL} {what}. The next engineer continues it from the "
            "handoff its engineer left.",
            "swarm",
        ),
    ]
    assert merging.shell.calls == []
    assert merging.store.handoff("sw", "t1") == "the handoff"
    assert merging.store.claimant("sw", "t1") is None
    assert ready.record(merging.store, "sw", "t1") is None


@pytest.mark.parametrize("failed, step", [(("Tests", "Lint"), "Tests, Lint"), (("Tests",), "Tests")])
def test_red_checks_reopen_the_task_awaiting_fix_with_the_failing_step_named(merging, failed, step):
    merging.pulls[URL] = PullRequest("OPEN", None, 1, True, True, failed=failed, head=HEAD)
    assert merging.run()[0] == f"task t1 reopened, {URL} has red checks: {step}"
    assert merging.rows["t1"]["awaiting"] == "fix"
    assert merging.ledger.comments == [
        (
            "t1",
            f"Reopened by the swarm: its pull request {URL} has red checks: {step}. The next engineer continues it "
            "from the handoff its engineer left.",
            "swarm",
        ),
    ]


def test_a_pull_request_that_conflicts_with_dev_reopens_instead_of_waiting_on_checks(merging):
    merging.pulls[URL] = PullRequest("OPEN", None, 1, False, False, head=HEAD, mergeable="CONFLICTING")
    what = "conflicts with dev and needs an update the swarm cannot make"
    assert merging.run() == [f"task t1 reopened, {URL} {what}"]
    assert merging.rows["t1"]["state"] == "open" and merging.rows["t1"]["awaiting"] == "fix"
    assert merging.queued == []
    assert ready.record(merging.store, "sw", "t1") is None


def test_a_conflict_while_queued_is_left_to_the_queue(merging):
    queued_at_now(merging)
    merging.pulls[URL] = PullRequest("OPEN", None, 1, False, True, head=HEAD, queued=True, mergeable="CONFLICTING")
    assert merging.run(NOW + 60_000) == []
    assert merging.rows["t1"]["state"] == "pr"


def test_an_enqueue_github_refuses_reopens_the_task_awaiting_fix(merging):
    merging.pulls[URL] = GREEN
    error = "GitHub refused to enqueue the pull request"

    def refused_enqueue(action, url, journal=None):
        raise SwarmError(error)

    assert merging.run(queuer=refused_enqueue) == [
        f"task t1 reopened, {URL} could not be queued: {error}",
    ]
    assert merging.rows["t1"]["state"] == "open" and merging.rows["t1"]["awaiting"] == "fix"


def test_a_pull_request_closed_while_queued_reopens_as_closed(merging):
    queued_at_now(merging)
    merging.pulls[URL] = CLOSED
    assert merging.run(NOW + 60_000)[0] == f"task t1 reopened, {URL} was closed without merging"


def test_a_reopen_leaves_a_task_the_ledger_already_moved_on(merging):
    merging.ledger.rows = {"t1": {**merging.rows["t1"], "state": "done"}}
    merging.pulls[URL] = RED
    merging.run()
    assert merging.ledger.rows["t1"]["state"] == "done" and merging.ledger.rows["t1"]["claimed_by"] == AGENT


@pytest.mark.parametrize("pull", [GREEN, RED], ids=["green-with-merge-group-failure", "red-checks"])
def test_a_pull_request_that_leaves_the_queue_unmerged_reopens_the_task(merging, pull):
    queued_at_now(merging)
    merging.pulls[URL] = QUEUED
    merging.run(NOW + 60_000)
    merging.pulls[URL] = pull
    assert merging.run(NOW + 120_000)[0] == f"task t1 reopened, {URL} left the merge queue without merging"
    assert merging.rows["t1"]["state"] == "open"
    assert merging.rows["t1"]["awaiting"] == "fix"
    assert merging.queued == [("queue", URL)]


def test_a_pull_request_that_leaves_the_queue_red_reopens_without_a_requeue(merging):
    queued_at_now(merging)
    merging.pulls[URL] = QUEUED
    merging.run(NOW + 60_000)
    merging.pulls[URL] = RED
    assert merging.run(NOW + 120_000)[0] == f"task t1 reopened, {URL} left the merge queue without merging"
    assert merging.exits.calls == [] and merging.queued == [("queue", URL)]
    assert merging.rows["t1"]["state"] == "open"


def left_queue_green(merging):
    queued_at_now(merging)
    merging.pulls[URL] = QUEUED
    merging.run(NOW + 60_000)
    merging.pulls[URL] = GREEN


def test_a_green_pull_request_dropped_with_no_failure_of_its_own_is_queued_again(merging):
    left_queue_green(merging)
    merging.exits.failures = []
    generation = merging.rows["t1"].get("claim_generation", 0)
    assert merging.run(NOW + 120_000) == [f"queued {URL} for task t1"]
    assert merging.exits.calls == [(URL, NOW)]
    assert merging.queued == [("queue", URL), ("queue", URL)]
    assert merging.rows["t1"]["state"] == "pr" and merging.rows["t1"]["claimed_by"] == AGENT
    assert merging.rows["t1"].get("claim_generation", 0) == generation
    assert merging.ledger.comments == [("t1", ready.REQUEUED.format(url=URL), "swarm")]
    found = ready.record(merging.store, "sw", "t1")
    assert found["requeues"] == 1 and found["queued_at"] == NOW + 120_000
    assert found["on"] == {"kind": "merge", "target": URL}


def test_each_accepted_requeue_adds_to_the_stored_count(merging):
    left_queue_green(merging)
    merging.exits.failures = []
    found = ready.record(merging.store, "sw", "t1")
    ready.save(merging.store, "sw", "t1", {**found, "requeues": 2})
    assert merging.run(NOW + 120_000) == [f"queued {URL} for task t1"]
    assert ready.record(merging.store, "sw", "t1")["requeues"] == 3


def test_a_first_requeue_is_allowed_under_a_cap_of_one(merging, monkeypatch):
    monkeypatch.setattr(ready, "QUEUE_TRIES", 1)
    left_queue_green(merging)
    merging.exits.failures = []
    assert merging.run(NOW + 120_000) == [f"queued {URL} for task t1"]
    assert ready.record(merging.store, "sw", "t1")["requeues"] == 1


def test_an_own_merge_group_failure_reopens_the_task(merging):
    left_queue_green(merging)
    assert merging.run(NOW + 120_000)[0] == f"task t1 reopened, {URL} left the merge queue without merging"
    assert merging.exits.calls == [(URL, NOW)]
    assert merging.queued == [("queue", URL)] and merging.rows["t1"]["state"] == "open"


def test_merge_group_runs_that_cannot_all_be_listed_reopen_the_task(merging):
    left_queue_green(merging)
    merging.exits.failures = None
    assert merging.run(NOW + 120_000)[0] == f"task t1 reopened, {URL} left the merge queue without merging"
    assert merging.rows["t1"]["state"] == "open"


def test_an_unreadable_merge_group_listing_holds_for_the_next_tick(merging):
    left_queue_green(merging)
    merging.exits.failures = SwarmError("timed out")
    assert merging.run(NOW + 120_000) == [f"could not read the merge group runs of {URL} for task t1: timed out"]
    assert merging.rows["t1"]["state"] == "pr" and merging.rows["t1"]["claimed_by"] == AGENT
    assert ready.record(merging.store, "sw", "t1")["on"]["kind"] == "merge"
    assert merging.queued == [("queue", URL)] and merging.ledger.comments == []


def test_a_pull_request_dropped_again_after_its_requeues_reopens_the_task(merging):
    left_queue_green(merging)
    merging.exits.failures = []
    found = ready.record(merging.store, "sw", "t1")
    ready.save(merging.store, "sw", "t1", {**found, "requeues": ready.QUEUE_TRIES})
    assert merging.run(NOW + 120_000)[0] == f"task t1 reopened, {URL} left the merge queue without merging"
    assert merging.queued == [("queue", URL)] and merging.rows["t1"]["state"] == "open"


def test_a_requeue_the_queue_refuses_records_no_requeue_comment(merging):
    left_queue_green(merging)
    merging.exits.failures = []

    def refuse(action, url, journal=None):
        raise SwarmError("timed out")

    for minute in range(ready.QUEUE_TRIES + 1):
        assert merging.run(NOW + 120_000 + minute * 60_000, refuse) == [f"could not queue {URL} for task t1: timed out"]
    assert merging.rows["t1"]["state"] == "pr" and merging.ledger.comments == []
    assert "requeues" not in ready.record(merging.store, "sw", "t1")


@pytest.mark.parametrize(
    "queued, mergeable, cause",
    [
        (True, "CONFLICTING", "has a merge conflict"),
        (False, "CONFLICTING", "has a merge conflict"),
    ],
)
def test_an_unmergeable_entry_reopens_with_its_cause_within_one_tick(merging, queued, mergeable, cause):
    from scripts.swarm.ledger_events import pull_request

    queued_at_now(merging)
    merging.pulls[URL] = pull_request(
        {
            "state": "OPEN",
            "mergeable": mergeable,
            "mergeQueueEntry": {"id": "entry", "state": "UNMERGEABLE"} if queued else None,
        }
    )

    def dequeue(action, url, journal=None):
        assert (action, url) == ("dequeue", URL)
        merging.queued.append((action, url))
        return {"queued": False}

    actions = merging.run(NOW + 60_000, queuer=dequeue)
    assert actions[0] == f"task t1 reopened, {URL} {cause}"
    assert merging.queued == [("queue", URL)] + ([("dequeue", URL)] if queued else [])
    assert merging.rows["t1"]["state"] == "open"
    assert merging.rows["t1"]["claimed_by"] == ""
    assert merging.rows["t1"]["awaiting"] == "fix"
    assert merging.store.claimant("sw", "t1") is None
    assert ready.record(merging.store, "sw", "t1") is None
    assert cause in merging.ledger.comments[0][1]
    assert merging.run(NOW + 120_000, queuer=dequeue) == []


def transient(queued=True, mergeable="MERGEABLE", head=HEAD):
    from scripts.swarm.ledger_events import pull_request

    return pull_request(
        {
            "state": "OPEN",
            "mergeable": mergeable,
            "headRefOid": head,
            "mergeQueueEntry": {"id": "entry", "state": "UNMERGEABLE"} if queued else None,
        }
    )


def watcher(merging, cached, fresh, now, store=None):
    run = ready.Pass(store or merging.store, "sw", merging.ledger, cached, fresh, now, merging.queue, merging.exits)
    return ready.merge_pass(run, merging.rows)


def dequeued_transient(merging):
    queued_at_now(merging)
    merging.queue.extra = {"queued": False}
    merging.pulls[URL] = transient()
    return merging.run(NOW + 60_000)


def test_a_stale_unmergeable_snapshot_is_not_dequeued_when_a_fresh_read_clears_it(merging):
    queued_at_now(merging)
    assert watcher(merging, {URL: transient()}.get, {URL: QUEUED}.get, NOW + 60_000) == []
    assert merging.queued == [("queue", URL)]
    assert merging.rows["t1"]["state"] == "pr" and merging.rows["t1"]["claimed_by"] == AGENT
    assert ready.record(merging.store, "sw", "t1")["on"] == {"kind": "merge", "target": URL}


def test_a_watcher_dequeue_hands_the_queue_a_journal_naming_swarm_task_and_timer(merging):
    import os

    dequeued_transient(merging)
    assert merging.queued == [("queue", URL), ("dequeue", URL)]
    journal = merging.queue.journals[-1]
    assert journal.url == URL
    assert journal.context == {"swarm": "sw", "task": "t1", "timer": os.getpid(), "by": "merge watcher"}


def watcher_journals(merging):
    import os

    context = {"swarm": "sw", "task": "t1", "timer": os.getpid(), "by": "merge watcher"}
    return [
        journal is not None and journal.url == URL and journal.context == context
        for (action, _), journal in zip(merging.queued, merging.queue.journals, strict=True)
        if action == "queue"
    ]


def test_a_watcher_queue_on_green_checks_hands_the_queue_its_journal(merging):
    queued_at_now(merging)
    assert watcher_journals(merging) == [True]


def test_a_watcher_requeue_after_a_green_drop_hands_the_queue_its_journal(merging):
    left_queue_green(merging)
    merging.exits.failures = []
    assert merging.run(NOW + 120_000) == [f"queued {URL} for task t1"]
    assert watcher_journals(merging) == [True, True]


def test_a_watcher_recovery_requeue_hands_the_queue_its_journal(merging):
    dequeued_transient(merging)
    merging.pulls[URL] = GREEN
    assert merging.run(ready.record(merging.store, "sw", "t1")["retry_at"]) == [f"queued {URL} for task t1"]
    assert merging.queued == [("queue", URL), ("dequeue", URL), ("queue", URL)]
    assert watcher_journals(merging) == [True, True]


def test_a_watcher_dequeue_writes_its_identity_into_the_queue_journal(env, monkeypatch, tmp_path):
    import os

    from scripts.swarm import queue_record
    from scripts.swarm.ledger_events import pull_request
    from tests.swarm.test_delivery import FakeHerdr

    store, ledger, rt = env
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path))
    cli.main(["sw", "create", "--repo", "/repo"])
    ledger.rows["t1"].update({"state": "pr", "pr_url": URL, "claimed_by": AGENT, "awaiting": "merge"})
    store.claim("sw", "t1", AGENT, 60_000)
    found = ready.held_record(URL, HEAD, TOP, AGENT, NOW)
    found["on"], found["queued_at"] = {"kind": "merge", "target": URL}, NOW
    ready.save(store, "sw", "t1", found)
    raw = {
        "id": "pull",
        "state": "OPEN",
        "baseRefName": "dev",
        "headRefOid": HEAD,
        "mergeable": "MERGEABLE",
        "mergeQueueEntry": {"id": "entry", "state": "UNMERGEABLE"},
    }
    ledger.pulls[URL] = pull_request(raw)

    def graphql(query, values, run):
        if query == queue_record.GROUPS:
            entries = {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}
            return {"viewer": {"login": "bot"}, "resource": {"repository": {"mergeQueue": {"entries": entries}}}}
        if query == ready.merge_queue.DEQUEUE:
            raw["mergeQueueEntry"] = None
            return {}
        return {"resource": dict(raw)}

    monkeypatch.setattr(ready.merge_queue, "graphql", graphql)
    monkeypatch.setattr(cli.ledger_events, "views", lambda urls, cache=None: {url: ledger.pulls[url] for url in urls})
    monkeypatch.setattr(cli.ledger_events, "view", lambda url, *args, **kwargs: pull_request(raw))
    monkeypatch.setattr(stack, "shell", Shell())
    monkeypatch.setattr(cli, "now_ms", lambda: NOW + 60_000)
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    records = [json.loads(line) for line in (tmp_path / "merge-queue-events.jsonl").read_text().splitlines()]
    assert [(r["action"], r["pull"], r["swarm"], r["task"], r["timer"], r["by"]) for r in records] == [
        ("dequeue", URL, "sw", "t1", os.getpid(), "merge watcher")
    ]


def test_a_transient_unmergeable_head_is_requeued_unchanged_after_a_timer_restart(merging):
    assert dequeued_transient(merging) == [f"dequeued {URL} for task t1: {ready.TRANSIENT}"]
    assert merging.ledger.comments == [("t1", ready.DEQUEUED.format(url=URL), "swarm")]
    assert ready.record(merging.store, "sw", "t1")["on"] == {"kind": ready.RECOVER, "head": HEAD}
    assert merging.rows["t1"]["state"] == "pr" and merging.rows["t1"]["claimed_by"] == AGENT
    assert merging.store.claimant("sw", "t1") == AGENT
    merging.pulls[URL] = GREEN
    restarted = RedisStore(merging.store.redis)
    assert watcher(merging, merging.pulls.get, merging.pulls.get, NOW + 60_000 + ready.BACKOFF_MS - 1, restarted) == []
    assert merging.queued == [("queue", URL), ("dequeue", URL)]
    assert watcher(merging, merging.pulls.get, merging.pulls.get, NOW + 60_000 + ready.BACKOFF_MS, restarted) == [
        f"queued {URL} for task t1"
    ]
    assert merging.queued == [("queue", URL), ("dequeue", URL), ("queue", URL)]
    assert ready.record(merging.store, "sw", "t1")["on"] == {"kind": "merge", "target": URL}
    assert merging.rows["t1"]["state"] == "pr" and merging.rows["t1"]["claimed_by"] == AGENT
    assert ready.record(merging.store, "sw", "t1")["queued_at"] == NOW + 60_000 + ready.BACKOFF_MS
    assert merging.ledger.comments[-1] == ("t1", ready.RECOVERED.format(url=URL), "swarm")
    assert merging.ledger.alerts == []


def test_a_transient_unmergeable_head_waits_with_a_bounded_backoff_then_reopens(merging):
    dequeued_transient(merging)
    merging.pulls[URL] = RUNNING
    now, waits_ms = NOW + 60_000, []
    for _ in range(ready.QUEUE_TRIES - 1):
        due = ready.record(merging.store, "sw", "t1")["retry_at"]
        waits_ms.append(due - now)
        now = due
        assert merging.run(now) == [f"{URL} for task t1 is not yet mergeable and green; it retries later"]
    assert waits_ms == [ready.BACKOFF_MS * 2**tries for tries in range(ready.QUEUE_TRIES - 1)]
    now = ready.record(merging.store, "sw", "t1")["retry_at"]
    assert merging.run(now)[0] == f"task t1 reopened, {URL} {ready.STUCK}"
    assert merging.rows["t1"]["state"] == "open" and merging.rows["t1"]["awaiting"] == "fix"
    assert merging.queued == [("queue", URL), ("dequeue", URL)]
    assert merging.ledger.alerts == [(ready.ALERT_SOURCE, ready.GAVE_UP.format(url=URL, task="t1"))]


def test_a_failed_requeue_raises_a_ledger_alert(merging):
    dequeued_transient(merging)
    merging.pulls[URL] = GREEN

    def refuse(action, url, journal=None):
        merging.queued.append((action, url))
        raise SwarmError("timed out")

    due = ready.record(merging.store, "sw", "t1")["retry_at"]
    assert merging.run(due, refuse) == [f"could not requeue {URL} for task t1: timed out"]
    assert merging.ledger.alerts == [
        (ready.ALERT_SOURCE, ready.REQUEUE_FAILED.format(url=URL, task="t1", why="timed out"))
    ]
    assert merging.rows["t1"]["state"] == "pr" and merging.rows["t1"]["claimed_by"] == AGENT
    assert ready.record(merging.store, "sw", "t1")["on"]["kind"] == "recover"


def test_a_real_conflict_after_a_transient_dequeue_still_becomes_a_repair_task(merging):
    dequeued_transient(merging)
    merging.pulls[URL] = transient(queued=False, mergeable="CONFLICTING")
    assert merging.run(NOW + 120_000)[0] == f"task t1 reopened, {URL} has a merge conflict"
    assert merging.rows["t1"]["state"] == "open" and merging.rows["t1"]["awaiting"] == "fix"
    assert merging.rows["t1"]["claimed_by"] == "" and merging.store.claimant("sw", "t1") is None
    assert merging.queued == [("queue", URL), ("dequeue", URL)]


def test_a_new_head_after_a_transient_dequeue_reopens_the_task(merging):
    dequeued_transient(merging)
    merging.pulls[URL] = PullRequest("OPEN", None, 1, False, True, head="b" * 40)
    due = ready.record(merging.store, "sw", "t1")["retry_at"]
    assert merging.run(due)[0] == f"task t1 reopened, {URL} {ready.MOVED}"
    assert merging.queued == [("queue", URL), ("dequeue", URL)]


def test_an_empty_head_read_at_the_dequeue_recovers_on_the_saved_head(merging):
    queued_at_now(merging)
    merging.queue.extra = {"queued": False}
    merging.pulls[URL] = transient(head="")
    assert merging.run(NOW + 60_000) == [f"dequeued {URL} for task t1: {ready.TRANSIENT}"]
    assert ready.record(merging.store, "sw", "t1")["on"] == {"kind": ready.RECOVER, "head": HEAD}
    merging.pulls[URL] = GREEN
    assert merging.run(ready.record(merging.store, "sw", "t1")["retry_at"]) == [f"queued {URL} for task t1"]
    assert merging.queued == [("queue", URL), ("dequeue", URL), ("queue", URL)]
    assert merging.rows["t1"]["state"] == "pr" and merging.rows["t1"]["claimed_by"] == AGENT


def test_an_empty_head_read_at_the_retry_requeues_the_saved_green_head(merging):
    dequeued_transient(merging)
    merging.pulls[URL] = PullRequest("OPEN", None, 1, False, True, head="")
    assert merging.run(ready.record(merging.store, "sw", "t1")["retry_at"]) == [f"queued {URL} for task t1"]
    assert merging.queued == [("queue", URL), ("dequeue", URL), ("queue", URL)]
    assert ready.record(merging.store, "sw", "t1")["on"] == {"kind": "merge", "target": URL}
    assert merging.ledger.comments[-1] == ("t1", ready.RECOVERED.format(url=URL), "swarm")


def test_an_empty_head_read_with_no_saved_head_reopens_for_repair(merging):
    queued_at_now(merging)
    found = ready.record(merging.store, "sw", "t1")
    del found["head"]
    ready.save(merging.store, "sw", "t1", found)
    merging.queue.extra = {"queued": False}
    merging.pulls[URL] = transient(head="")
    assert merging.run(NOW + 60_000) == [f"task t1 reopened, {URL} is marked unmergeable by GitHub"]
    assert merging.rows["t1"]["state"] == "open" and merging.rows["t1"]["awaiting"] == "fix"
    assert merging.queued == [("queue", URL), ("dequeue", URL)]


def test_a_read_head_at_the_dequeue_wins_over_the_saved_head(merging):
    queued_at_now(merging)
    merging.queue.extra = {"queued": False}
    merging.pulls[URL] = transient(head="c" * 40)
    assert merging.run(NOW + 60_000) == [f"dequeued {URL} for task t1: {ready.TRANSIENT}"]
    assert ready.record(merging.store, "sw", "t1")["on"] == {"kind": ready.RECOVER, "head": "c" * 40}


@pytest.mark.parametrize(
    "fresh, cause",
    [
        (PullRequest("OPEN", None, 1, True, True, head=HEAD, failed=("unit",)), "has red checks"),
        (
            PullRequest("OPEN", None, 1, False, True, head=HEAD, mergeable="CONFLICTING", merge_problem="conflicts"),
            "conflicts",
        ),
    ],
)
def test_a_fresh_read_at_the_retry_that_shows_a_repair_need_reopens_the_task(merging, fresh, cause):
    dequeued_transient(merging)
    due = ready.record(merging.store, "sw", "t1")["retry_at"]
    assert watcher(merging, {URL: GREEN}.get, {URL: fresh}.get, due)[0] == f"task t1 reopened, {URL} {cause}"
    assert merging.rows["t1"]["state"] == "open" and merging.rows["t1"]["awaiting"] == "fix"
    assert merging.queued == [("queue", URL), ("dequeue", URL)]


def test_a_head_that_turns_unmergeable_after_every_requeue_reopens_after_bounded_recoveries(merging):
    queued_at_now(merging)
    merging.queue.extra = {"queued": False}
    now, waits_ms = NOW, []
    for _ in range(ready.QUEUE_TRIES):
        now += 60_000
        merging.pulls[URL] = transient()
        assert merging.run(now) == [f"dequeued {URL} for task t1: {ready.TRANSIENT}"]
        due = ready.record(merging.store, "sw", "t1")["retry_at"]
        waits_ms.append(due - now)
        now = due
        merging.pulls[URL] = GREEN
        assert merging.run(now) == [f"queued {URL} for task t1"]
    assert waits_ms == [ready.BACKOFF_MS * 2**tries for tries in range(ready.QUEUE_TRIES)]
    merging.pulls[URL] = transient()
    assert merging.run(now + 60_000)[0] == f"task t1 reopened, {URL} {ready.STUCK}"
    assert merging.rows["t1"]["state"] == "open" and merging.rows["t1"]["awaiting"] == "fix"
    assert merging.ledger.alerts == [(ready.ALERT_SOURCE, ready.GAVE_UP.format(url=URL, task="t1"))]


def test_an_unmergeable_entry_on_a_task_outside_the_merge_pass_reopens_with_its_cause(merging):
    merging.rows["t1"]["awaiting"] = ""
    ready.forget(merging.store, "sw", "t1")
    merging.pulls[URL] = transient()
    merging.queue.extra = {"queued": False}
    assert merging.run()[0] == f"task t1 reopened, {URL} is marked unmergeable by GitHub"
    assert merging.queued == [("dequeue", URL)]
    assert merging.rows["t1"]["state"] == "open" and merging.rows["t1"]["claimed_by"] == ""


@pytest.mark.parametrize("queued", [True, False])
def test_a_conflict_reopens_a_task_whose_engineer_has_not_released_its_seat(merging, queued):
    from scripts.swarm.ledger_events import pull_request

    merging.rows["t1"]["awaiting"] = ""
    ready.forget(merging.store, "sw", "t1")
    merging.pulls[URL] = pull_request(
        {
            "state": "OPEN",
            "mergeable": "CONFLICTING",
            "mergeQueueEntry": {"id": "entry", "state": "UNMERGEABLE"} if queued else None,
        }
    )
    merging.queue.extra = {"queued": False}
    assert merging.run()[0] == f"task t1 reopened, {URL} has a merge conflict"
    assert merging.queued == ([("dequeue", URL)] if queued else [])
    assert merging.rows["t1"]["state"] == "open"
    assert merging.rows["t1"]["claimed_by"] == ""
    assert merging.store.claimant("sw", "t1") is None
    assert "has a merge conflict" in merging.ledger.comments[0][1]
    assert merging.shell.calls == []


@pytest.mark.parametrize(("name", "task"), [(AGENT, "other-task"), ("unrelated-worker", "other-task")])
def test_a_reopen_does_not_retain_an_unrelated_registered_worker(merging, name, task):
    from scripts.swarm.store import AgentRecord

    merging.store.put_agent("sw", AgentRecord(name, "eng", task))
    merging.pulls[URL] = RED
    merging.run()
    assert merging.rows["t1"]["state"] == "open"
    assert merging.rows["t1"]["claimed_by"] == ""
    assert merging.store.claimant("sw", "t1") is None
    assert [(agent.name, agent.task) for agent in merging.store.agents("sw")] == [(name, task)]


@pytest.mark.parametrize("queued", [True, False])
@pytest.mark.parametrize("awaiting", ["", "merge"])
def test_a_working_owner_keeps_its_claim_and_lease_when_the_pull_conflicts(merging, queued, awaiting):
    from scripts.inbox.store import InboxStore
    from scripts.swarm.ledger_events import pull_request
    from scripts.swarm.store import AgentRecord

    merging.rows["t1"]["awaiting"] = awaiting
    merging.store.put_agent("sw", AgentRecord(AGENT, "eng", "t1", state="working"))
    merging.pulls[URL] = pull_request(
        {
            "state": "OPEN",
            "mergeable": "CONFLICTING",
            "mergeQueueEntry": {"id": "entry", "state": "UNMERGEABLE"} if queued else None,
        }
    )
    merging.queue.extra = {"queued": False}
    merging.run()
    assert merging.rows["t1"]["state"] == "pr"
    assert merging.rows["t1"]["claimed_by"] == AGENT
    assert merging.rows["t1"]["awaiting"] == awaiting
    assert merging.store.claimant("sw", "t1") == AGENT
    assert merging.shell.calls == []
    assert merging.queued == []
    messages = InboxStore(merging.store.redis).inbox(AGENT)
    assert len(messages) == 1
    assert messages[0].sender == "swarm"
    assert messages[0].task == "t1"
    assert "restack" in messages[0].text.lower()
    merging.run()
    assert len(InboxStore(merging.store.redis).inbox(AGENT)) == 1


@pytest.mark.parametrize("successor", [AGENT, "successor"])
def test_a_stale_conflict_watch_cannot_clear_a_new_claim_generation(merging, successor):
    from scripts.swarm.ledger_events import pull_request

    merging.rows["t1"]["claim_generation"] = 1
    merging.pulls[URL] = pull_request(
        {"state": "OPEN", "mergeable": "CONFLICTING", "mergeQueueEntry": {"id": "entry", "state": "UNMERGEABLE"}}
    )

    def replace_owner(action, url, journal=None):
        merging.rows["t1"] = {**merging.rows["t1"], "claimed_by": successor, "claim_generation": 2}
        merging.store.release("sw", "t1", AGENT)
        merging.store.claim("sw", "t1", successor, 60_000)
        ready.save(merging.store, "sw", "t1", {"agent": successor, "head": "new-head"})
        return {"queued": False}

    merging.run(queuer=replace_owner)
    assert merging.rows["t1"]["state"] == "pr"
    assert merging.rows["t1"]["claimed_by"] == successor
    assert merging.rows["t1"]["claim_generation"] == 2
    assert merging.store.claimant("sw", "t1") == successor
    assert ready.record(merging.store, "sw", "t1") == {"agent": successor, "head": "new-head"}
    assert merging.ledger.comments == []


def test_a_successor_claim_after_the_reopen_write_keeps_its_ready_record_and_worktree(merging, monkeypatch):
    update = merging.ledger.update_task
    merging.pulls[URL] = RED

    def takeover(*args, **kwargs):
        result = update(*args, **kwargs)
        merging.store.release("sw", "t1", AGENT)
        merging.store.claim("sw", "t1", "successor", 60_000)
        merging.rows["t1"] = {**result, "state": "pr", "claimed_by": "successor", "claim_generation": 2}
        ready.save(merging.store, "sw", "t1", {"agent": "successor", "head": "new-head", "worktree": TOP})
        return result

    monkeypatch.setattr(merging.ledger, "update_task", takeover)
    assert merging.run() == []
    assert merging.store.claimant("sw", "t1") == "successor"
    assert ready.record(merging.store, "sw", "t1") == {"agent": "successor", "head": "new-head", "worktree": TOP}
    assert merging.shell.calls == []
    assert merging.ledger.comments == []


@pytest.mark.parametrize("generation", [0, 7])
def test_a_released_claim_reopens_at_its_current_generation(merging, generation):
    merging.rows["t1"]["claim_generation"] = generation
    merging.pulls[URL] = RED
    assert merging.run() == [f"task t1 reopened, {URL} has red checks"]
    assert merging.rows["t1"]["claim_generation"] == generation + 1


def test_an_explicitly_unowned_legacy_pull_reopens_without_a_generation(merging):
    from scripts.swarm.ledger_events import pull_request

    merging.rows["t1"].update(claimed_by="", awaiting="")
    merging.store.release("sw", "t1", AGENT)
    ready.forget(merging.store, "sw", "t1")
    merging.pulls[URL] = pull_request({"state": "OPEN", "mergeable": "CONFLICTING"})
    assert merging.run() == [f"task t1 reopened, {URL} has a merge conflict"]
    assert merging.rows["t1"]["state"] == "open"
    assert merging.ledger.comments


@pytest.mark.parametrize("field,value", [("pr_url", "https://github.com/o/r/pull/99"), ("awaiting", "fix")])
def test_a_changed_pull_binding_survives_the_stale_conflict_watch(merging, field, value):
    from scripts.swarm.ledger_events import pull_request

    merging.pulls[URL] = pull_request(
        {"state": "OPEN", "mergeable": "CONFLICTING", "mergeQueueEntry": {"id": "entry", "state": "UNMERGEABLE"}}
    )

    def changed(action, url, journal=None):
        merging.rows["t1"] = {**merging.rows["t1"], field: value}
        return {"queued": False}

    assert merging.run(queuer=changed) == []
    assert merging.rows["t1"]["state"] == "pr"
    assert merging.rows["t1"][field] == value
    assert merging.store.claimant("sw", "t1") == AGENT
    assert ready.record(merging.store, "sw", "t1") is not None


@pytest.mark.parametrize("state,owner,generation", [("open", "successor", 2), ("open", "", 3), ("blocked", "", 2)])
def test_a_reopen_rejected_by_the_ledger_never_cleans_up_current_ownership(
    merging, monkeypatch, state, owner, generation
):
    merging.rows["t1"]["claim_generation"] = 1
    merging.pulls[URL] = RED

    def changed(*args, **kwargs):
        live = {**merging.rows["t1"], "state": state, "claimed_by": owner, "claim_generation": generation}
        merging.rows["t1"] = live
        return dict(live)

    monkeypatch.setattr(merging.ledger, "update_task", changed)
    assert merging.run() == []
    assert merging.store.claimant("sw", "t1") == AGENT
    assert ready.record(merging.store, "sw", "t1") is not None
    assert merging.ledger.comments == []


def test_an_owner_registered_during_dequeue_keeps_its_claim(merging):
    from scripts.swarm.ledger_events import pull_request
    from scripts.swarm.store import AgentRecord

    merging.pulls[URL] = pull_request(
        {"state": "OPEN", "mergeable": "CONFLICTING", "mergeQueueEntry": {"id": "entry", "state": "UNMERGEABLE"}}
    )

    def registered(action, url, journal=None):
        merging.store.put_agent("sw", AgentRecord(AGENT, "eng", "t1"))
        return {"queued": False}

    assert merging.run(queuer=registered) == []
    assert merging.rows["t1"]["claimed_by"] == AGENT
    assert merging.rows["t1"]["state"] == "pr"
    assert merging.store.claimant("sw", "t1") == AGENT


def test_a_finished_owner_does_not_hold_conflict_recovery(merging):
    from scripts.swarm.ledger_events import pull_request
    from scripts.swarm.store import AgentRecord

    merging.store.put_agent("sw", AgentRecord(AGENT, "eng", "t1", state="finished"))
    merging.pulls[URL] = pull_request({"state": "OPEN", "mergeable": "CONFLICTING"})
    assert merging.run() == [f"task t1 reopened, {URL} has a merge conflict"]
    assert merging.rows["t1"]["claimed_by"] == ""


@pytest.mark.parametrize("backend,target", [("kubernetes", {}), ("local", {"host_id": "remote-host"})])
def test_a_finished_remote_owner_keeps_its_claim_until_its_runtime_retires_it(merging, monkeypatch, backend, target):
    from scripts.swarm.store import AgentRecord

    agent = AgentRecord(AGENT, "eng", "t1", state="finished", runtime_backend=backend, runtime_target=target)
    monkeypatch.setattr(merging.store, "agents", lambda slug: [agent])
    merging.pulls[URL] = RED
    assert merging.run() == []
    assert merging.rows["t1"]["claimed_by"] == AGENT
    assert merging.store.claimant("sw", "t1") == AGENT
    assert ready.record(merging.store, "sw", "t1") is not None


def test_a_new_conflicting_head_gets_its_own_restack_notice(merging):
    from dataclasses import replace

    from scripts.inbox.store import InboxStore
    from scripts.swarm.ledger_events import pull_request
    from scripts.swarm.store import AgentRecord

    merging.store.put_agent("sw", AgentRecord(AGENT, "eng", "t1"))
    merging.pulls[URL] = pull_request({"state": "OPEN", "mergeable": "CONFLICTING", "headRefOid": "first-head"})
    merging.run()
    merging.pulls[URL] = replace(merging.pulls[URL], head="second-head")
    merging.run()
    assert len(InboxStore(merging.store.redis).inbox(AGENT)) == 2


def test_one_tick_reports_every_unreleased_entry_it_dequeues(merging):
    from scripts.swarm.ledger_events import pull_request

    other_url = URL + "0"
    merging.rows["t1"]["awaiting"] = ""
    merging.rows["t2"].update({"state": "pr", "awaiting": "", "claimed_by": "another engineer", "pr_url": other_url})
    merging.store.claim("sw", "t2", "another engineer", 60_000)
    pull = pull_request({"state": "OPEN", "mergeQueueEntry": {"id": "entry", "state": "UNMERGEABLE"}})
    merging.pulls.update({URL: pull, other_url: pull})
    merging.queue.extra = {"queued": False}
    assert merging.run() == [
        f"task t1 reopened, {URL} is marked unmergeable by GitHub",
        f"task t2 reopened, {other_url} is marked unmergeable by GitHub",
    ]
    assert merging.queued == [("dequeue", URL), ("dequeue", other_url)]
    assert all(row["state"] == "open" and row["claimed_by"] == "" for row in merging.rows.values())
    assert merging.store.claimant("sw", "t1") is None
    assert merging.store.claimant("sw", "t2") is None
    assert len(merging.ledger.comments) == 2


def test_an_unowned_task_without_a_claimant_field_can_leave_the_queue(merging):
    from scripts.swarm.ledger_events import pull_request

    other_url = URL + "0"
    merging.rows["t2"].update({"state": "pr", "pr_url": other_url})
    merging.rows["t2"].pop("claimed_by")
    merging.pulls[other_url] = pull_request(
        {"state": "OPEN", "mergeQueueEntry": {"id": "entry", "state": "UNMERGEABLE"}}
    )
    merging.queue.extra = {"queued": False}
    assert merging.run() == [f"task t2 reopened, {other_url} is marked unmergeable by GitHub"]
    assert merging.rows["t2"]["state"] == "open"
    assert merging.rows["t2"]["claimed_by"] == ""
    assert merging.queued == [("dequeue", other_url)]


@pytest.mark.parametrize("failure", ["GitHub did not answer", "still queued"])
def test_a_dequeue_that_does_not_finish_keeps_the_task_for_the_next_tick(merging, failure):
    from scripts.swarm.ledger_events import pull_request

    queued_at_now(merging)
    merging.pulls[URL] = pull_request({"state": "OPEN", "mergeQueueEntry": {"id": "entry", "state": "UNMERGEABLE"}})

    def dequeue(action, url, journal=None):
        assert (action, url) == ("dequeue", URL)
        if failure != "still queued":
            raise SwarmError(failure)
        return {"queued": True}

    assert merging.run(NOW + 60_000, queuer=dequeue)
    assert merging.rows["t1"]["state"] == "pr"
    assert merging.rows["t1"]["claimed_by"] == AGENT
    assert ready.record(merging.store, "sw", "t1")["on"]["kind"] == "merge"
    assert merging.ledger.comments == []
    merging.queue.extra = {"queued": False}
    assert merging.run(NOW + 120_000) == [f"dequeued {URL} for task t1: {ready.TRANSIENT}"]


def test_a_merge_racing_the_dequeue_is_confirmed_before_the_task_finishes(merging):
    from scripts.swarm.ledger_events import pull_request

    queued_at_now(merging)
    merging.pulls[URL] = pull_request({"state": "OPEN", "mergeQueueEntry": {"id": "entry", "state": "UNMERGEABLE"}})
    merging.queue.extra = {"state": "MERGED", "queued": False}
    assert merging.run(NOW + 60_000) == [f"dequeue of {URL} for task t1 awaits confirmation"]
    assert merging.rows["t1"]["state"] == "pr"
    assert merging.ledger.comments == []
    merging.pulls[URL] = MERGED
    assert merging.run(NOW + 120_000)[0] == f"task t1 done, {URL} merged"
    assert merging.rows["t1"]["state"] == "done"


@pytest.mark.parametrize("entry_state", ["QUEUED", "AWAITING_CHECKS", "MERGEABLE", "LOCKED", None])
@pytest.mark.parametrize("released", [True, False])
def test_a_queue_entry_without_a_known_merge_problem_keeps_its_place(merging, entry_state, released):
    from scripts.swarm.ledger_events import pull_request

    queued_at_now(merging)
    if not released:
        merging.rows["t1"]["awaiting"] = ""
    merging.pulls[URL] = pull_request(
        {"state": "OPEN", "mergeable": "UNKNOWN", "mergeQueueEntry": {"id": "entry", "state": entry_state}}
    )
    assert merging.run(NOW + 60_000) == []
    assert merging.rows["t1"]["state"] == "pr"
    assert merging.rows["t1"]["claimed_by"] == AGENT
    assert merging.queued == [("queue", URL)]
    assert merging.ledger.comments == []


def test_an_unreleased_task_is_preserved_when_github_cannot_answer(merging):
    merging.rows["t1"]["awaiting"] = ""
    merging.pulls[URL] = None
    assert merging.run() == []
    assert merging.rows["t1"]["state"] == "pr"
    assert merging.rows["t1"]["claimed_by"] == AGENT
    assert merging.queued == []


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


def test_a_planted_unmergeable_entry_leaves_the_queue_within_two_full_ticks(env, monkeypatch):
    from scripts.swarm.ledger_events import pull_request
    from tests.swarm.test_delivery import FakeHerdr

    store, ledger, rt = env
    cli.main(["sw", "create", "--repo", "/repo"])
    ledger.rows["t1"].update({"state": "pr", "pr_url": URL, "claimed_by": AGENT, "awaiting": "merge"})
    store.claim("sw", "t1", AGENT, 60_000)
    found = ready.held_record(URL, HEAD, TOP, AGENT, NOW)
    found["on"], found["queued_at"] = {"kind": "merge", "target": URL}, NOW
    ready.save(store, "sw", "t1", found)
    raw = {
        "id": "pull",
        "state": "OPEN",
        "baseRefName": "dev",
        "headRefOid": HEAD,
        "mergeable": "CONFLICTING",
        "mergeQueueEntry": {"id": "plant", "state": "UNMERGEABLE"},
    }
    ledger.pulls[URL] = pull_request(raw)
    dequeued = []

    def graphql(query, values, run):
        if query == ready.queue_record.GROUPS:
            entries = {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None}}
            return {"viewer": {"login": "bot"}, "resource": {"repository": {"mergeQueue": {"entries": entries}}}}
        if query == ready.merge_queue.DEQUEUE:
            assert values == {"id": "pull"}
            dequeued.append(values["id"])
            raw["mergeQueueEntry"] = None
            ledger.pulls[URL] = pull_request(raw)
            return {}
        assert query == ready.merge_queue.STATE
        return {"resource": dict(raw)}

    monkeypatch.setattr(ready.merge_queue, "graphql", graphql)
    monkeypatch.setattr(cli.ledger_events, "views", lambda urls, cache=None: {url: ledger.pulls[url] for url in urls})
    monkeypatch.setattr(cli.ledger_events, "view", lambda url, *args, **kwargs: ledger.pulls.get(url))
    monkeypatch.setattr(stack, "shell", Shell())
    for tick_number in (1, 2):
        monkeypatch.setattr(cli, "now_ms", lambda: NOW + tick_number * 60_000)
        cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
        if ledger.rows["t1"]["state"] == "open":
            break
    assert tick_number == 1
    assert dequeued == ["pull"]
    assert raw["mergeQueueEntry"] is None
    assert ledger.rows["t1"]["state"] == "open"
    assert ledger.rows["t1"]["claimed_by"] == ""
    assert any("has a merge conflict" in text for _, text, _ in ledger.comments)


def test_the_ready_usage_names_its_handoff_document(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["sw", "ready", "--help"])
    assert "doc" in capsys.readouterr().out


def test_the_merge_pass_queues_only_once_a_fresh_read_confirms_green(merging):
    def run(cached, fresh):
        return ready.merge_pass(
            ready.Pass(merging.store, "sw", merging.ledger, {URL: cached}.get, {URL: fresh}.get, NOW, merging.queue),
            merging.rows,
        )

    assert run(GREEN, RUNNING) == []
    assert run(RUNNING, GREEN) == []
    assert merging.queued == []
    assert run(GREEN, GREEN) == [f"queued {URL} for task t1"]
    assert merging.queued == [("queue", URL)]
