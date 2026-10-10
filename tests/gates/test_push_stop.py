import re
import subprocess
import time
from pathlib import Path

import pytest

from scripts.gates import Call, Gate, Who, push_stop
from scripts.gates.progress import Progress
from scripts.gates.push_stop import (
    FLOOR_S,
    GATE_FAILED,
    GATE_LATE,
    GATE_SLOW,
    RESERVE_S,
    TEMPLATE,
    PushStop,
    count,
    record_text,
)
from scripts.gates.verdicts import Verdicts
from scripts.inbox.store import InboxStore
from scripts.swarm.naming import plain
from scripts.swarm.store import RedisStore, SwarmError

pytestmark = pytest.mark.xdist_group("fakeredis")

SLUG, ME = "demo", "engineer@100001-0001"
BRANCH = "engineer-100001-0001"
WHO = Who(name=ME, swarm=SLUG, lane="eng", task="t1")
STOP = Call("")
NOW = 5_000_000
IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@example.com")


def git(path, *args):
    done = subprocess.run(["git", "-C", str(path), *IDENTITY, *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


class FakeLedger:
    def __init__(self, task):
        self.task, self.events, self.comments, self.delay = task, [], [], 0

    def state(self, slug):
        return {"tasks": [self.task] if slug == SLUG else [], "_meta": {"events": self.events}}

    def comment(self, slug, task_id, text, by):
        time.sleep(self.delay)
        self.comments.append((slug, task_id, text, by))


@pytest.fixture
def rig(tmp_path):
    import fakeredis

    origin, seed, root = tmp_path / "origin.git", tmp_path / "seed", tmp_path / "worktrees"
    git(tmp_path, "init", "--bare", "-b", "dev", str(origin))
    git(tmp_path, "clone", str(origin), str(seed))
    (seed / "readme").write_text("seed\n")
    git(seed, "add", "readme")
    git(seed, "commit", "-m", "seed")
    git(seed, "push", "origin", "HEAD:refs/heads/dev")
    tree = root / "repo" / BRANCH
    git(seed, "fetch", "origin")
    git(seed, "worktree", "add", "-b", BRANCH, str(tree), "origin/dev")
    git(seed, "remote", "set-url", "--push", "origin", str(origin))
    git(seed, "remote", "set-url", "origin", "https://github.com/o/r.git")
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    ledger = FakeLedger({"id": "t1", "state": "claimed", "claimed_by": ME, "pr_url": "", "kind": "code"})
    ticks = []
    gate = PushStop(
        connect=lambda: store,
        ledger=lambda: ledger,
        root=root,
        now=lambda: NOW,
        clock=lambda: ticks.pop(0) if ticks else time.monotonic(),
    )

    def stop(who=WHO, started=None):
        gate.started = time.monotonic() if started is None else started
        return gate.decide(STOP, who, Verdicts(SLUG, gate.name, tmp_path))

    def commit(name="work"):
        (tree / name).write_text(name)
        git(tree, "add", name)
        git(tree, "commit", "-m", name)
        return git(tree, "rev-parse", "HEAD")

    def remote_head():
        return git(tmp_path, "ls-remote", str(origin), f"refs/heads/{BRANCH}").split("\t")[0]

    def inbox():
        return [item.text for item in InboxStore(store.redis).inbox(ME)]

    rig = type("Rig", (), {})()
    rig.store, rig.ledger, rig.gate, rig.tree, rig.origin, rig.root = store, ledger, gate, tree, origin, root
    rig.stop, rig.commit, rig.remote_head, rig.inbox, rig.seed = stop, commit, remote_head, inbox, seed
    rig.ticks = ticks
    return rig


def test_it_is_a_stop_gate_that_ships_enforcing():
    gate = PushStop()
    assert isinstance(gate, Gate)
    assert gate.name == "push-stop"
    assert gate.default_mode == "enforce"
    assert gate.matches(Call(""))
    assert not gate.matches(Call("Bash"))


def test_the_template_is_the_operator_text():
    assert TEMPLATE == (
        "You cannot leave uncommitted or unpushed changes. Commit them now. If the task is done, open the pull "
        "request. If not, update the issue and the pull request and record progress on the ledger."
    )


def test_a_fresh_worktree_with_no_work_stops_freely(rig):
    assert rig.stop().allowed
    assert rig.remote_head() == ""
    assert rig.inbox() == []


def test_commits_ahead_of_origin_are_pushed_by_the_hook_and_recorded_on_the_task(rig):
    rig.ledger.task["pr_url"] = "https://github.com/o/r/pull/7"
    head = rig.commit()
    assert rig.stop().allowed
    assert rig.remote_head() == head
    assert git(rig.tree, "rev-parse", "--abbrev-ref", "@{upstream}") == f"origin/{BRANCH}"
    record = (
        f"The stop hook pushed the task branch https://github.com/o/r/tree/{BRANCH} with its head at "
        f"https://github.com/o/r/commit/{head}"
    )
    assert rig.ledger.comments == [("demo", "t1", record, "swarm")]
    mark = Progress(rig.store.redis, SLUG).read(ME)
    assert (mark.outcome, mark.outcome_at) == ("pushed", NOW)


def test_a_clean_pushed_worktree_stops_freely_and_pushes_nothing_again(rig):
    rig.ledger.task["pr_url"] = "https://github.com/o/r/pull/7"
    rig.commit()
    assert rig.stop().allowed
    rig.ledger.comments.clear()
    assert rig.stop().allowed
    assert rig.ledger.comments == []


def test_uncommitted_changes_refuse_the_stop_with_the_template_once_in_the_inbox(rig):
    rig.ledger.task["pr_url"] = "https://github.com/o/r/pull/7"
    (rig.tree / "readme").write_text("changed\n")
    decision = rig.stop()
    assert (decision.allowed, decision.reason) == (False, TEMPLATE)
    assert rig.stop().reason == TEMPLATE
    assert rig.inbox() == [TEMPLATE]


def test_an_untracked_file_counts_as_uncommitted(rig):
    rig.ledger.task["pr_url"] = "https://github.com/o/r/pull/7"
    (rig.tree / "new").write_text("new\n")
    assert rig.stop().reason == TEMPLATE


def test_dirty_work_still_pushes_the_commits_it_holds(rig):
    rig.ledger.task["pr_url"] = "https://github.com/o/r/pull/7"
    head = rig.commit()
    (rig.tree / "new").write_text("new\n")
    assert rig.stop().reason == TEMPLATE
    assert rig.remote_head() == head


def test_a_closed_template_item_is_sent_again(rig):
    rig.ledger.task["pr_url"] = "https://github.com/o/r/pull/7"
    (rig.tree / "new").write_text("new\n")
    rig.stop()
    inbox = InboxStore(rig.store.redis)
    item = inbox.inbox(ME)[0]
    inbox.close(item.id, ME, "done", "committed")
    rig.stop()
    assert rig.inbox() == [TEMPLATE, TEMPLATE]


def test_a_later_stop_that_passes_closes_the_open_template_item_with_its_outcome(rig):
    rig.ledger.task["pr_url"] = "https://github.com/o/r/pull/7"
    (rig.tree / "new").write_text("new\n")
    rig.stop()
    git(rig.tree, "add", "new")
    git(rig.tree, "commit", "-m", "new")
    assert rig.stop().allowed
    [item] = InboxStore(rig.store.redis).inbox(ME)
    assert (item.state, item.reason) == ("done", f"done: {push_stop.SETTLED}")
    assert rig.stop().allowed


SEAT = "eng-1@demo"


def refused_once(rig):
    rig.ledger.task["pr_url"] = "https://github.com/o/r/pull/7"
    (rig.tree / "new").write_text("new\n")
    assert not rig.stop().allowed
    [item] = InboxStore(rig.store.redis).inbox(ME)
    return item


@pytest.mark.parametrize("seat", [SEAT, ""])
def test_an_agent_leaving_with_unpushed_work_closes_its_notice_naming_the_branch_not_pushed(monkeypatch, rig, seat):
    from scripts.inbox import exits

    monkeypatch.setenv("WORKTREE_ROOT", str(rig.root))
    item = refused_once(rig)
    inbox = InboxStore(rig.store.redis)
    exits.settle(inbox, ME, seat, "handed off its seat")
    closed = inbox.get(item.id)
    assert (closed.address, closed.state) == (ME, "done")
    assert closed.reason == f"done: {ME} handed off its seat; its branch {BRANCH} was not pushed"
    assert inbox.inbox(SEAT) == []
    assert inbox.pending_items("swarm") == []


def test_an_agent_leaving_after_its_work_reached_origin_closes_its_notice_naming_the_branch_pushed(monkeypatch, rig):
    from scripts.inbox import exits

    monkeypatch.setenv("WORKTREE_ROOT", str(rig.root))
    item = refused_once(rig)
    git(rig.tree, "add", "new")
    git(rig.tree, "commit", "-m", "new")
    git(rig.tree, "push", "origin", f"HEAD:refs/heads/{BRANCH}")
    inbox = InboxStore(rig.store.redis)
    exits.settle(inbox, ME, SEAT, "retired after the swarm idle limit")
    closed = inbox.get(item.id)
    assert closed.state == "done"
    assert closed.reason == f"done: {ME} retired after the swarm idle limit; its branch {BRANCH} was pushed"
    assert inbox.mailbox(SEAT) == []


def test_an_agent_leaving_with_no_branch_of_its_own_closes_its_notice_saying_so(monkeypatch, rig, tmp_path):
    from scripts.inbox import exits

    item = refused_once(rig)
    monkeypatch.setenv("WORKTREE_ROOT", str(tmp_path / "empty"))
    inbox = InboxStore(rig.store.redis)
    exits.settle(inbox, ME, SEAT, "exited")
    assert (
        inbox.get(item.id).reason
        == f"done: {ME} exited; its worktree was not found, so whether its branch was pushed is unknown"
    )


def test_an_agent_leaving_names_every_branch_it_left_off_origin(monkeypatch, rig):
    from scripts.inbox import exits

    monkeypatch.setenv("WORKTREE_ROOT", str(rig.root))
    item = refused_once(rig)
    second = rig.root / "repo" / f"{BRANCH}-2"
    git(rig.seed, "worktree", "add", "-b", f"{BRANCH}-2", str(second), "origin/dev")
    (second / "more").write_text("more\n")
    inbox = InboxStore(rig.store.redis)
    exits.settle(inbox, ME, SEAT, "exited")
    assert inbox.get(item.id).reason == f"done: {ME} exited; its branch {BRANCH}, {BRANCH}-2 was not pushed"


def test_other_mail_of_a_leaving_agent_still_moves_to_its_seat(monkeypatch, rig):
    from scripts.inbox import exits

    monkeypatch.setenv("WORKTREE_ROOT", str(rig.root))
    refused_once(rig)
    inbox = InboxStore(rig.store.redis)
    other = inbox.send("swarm", ME, "Your pull request checks failed.")
    exits.settle(inbox, ME, SEAT, "handed off its seat")
    assert [(item.id, item.state) for item in inbox.inbox(SEAT)] == [(other.id, "pending")]


def test_a_live_agents_notice_stays_open_until_a_stop_passes(rig):
    item = refused_once(rig)
    assert InboxStore(rig.store.redis).get(item.id).state not in ("done", "cancelled")
    assert not rig.stop().allowed
    git(rig.tree, "add", "new")
    git(rig.tree, "commit", "-m", "new")
    assert rig.stop().allowed
    assert InboxStore(rig.store.redis).get(item.id).reason == f"done: {push_stop.SETTLED}"


def test_pushed_work_without_a_pull_request_or_a_line_since_the_push_is_refused(rig):
    rig.commit()
    decision = rig.stop()
    assert (decision.allowed, decision.reason) == (False, TEMPLATE)
    assert rig.remote_head()
    assert rig.inbox() == [TEMPLATE]


def test_a_ledger_line_by_the_agent_after_the_push_lets_the_stop_through(rig):
    rig.commit()
    rig.stop()
    rig.ledger.events.append({"by": "swarm", "at": NOW + 1, "kind": "comment added"})
    rig.ledger.events.append({"by": ME, "at": NOW, "kind": "comment added"})
    assert not rig.stop().allowed
    rig.ledger.events.append({"by": ME, "at": NOW + 1, "kind": "comment added"})
    assert rig.stop().allowed


def test_a_push_the_agent_made_itself_counts_from_its_recorded_outcome(rig):
    rig.commit()
    git(rig.tree, "push", "-u", "origin", BRANCH)
    Progress(rig.store.redis, SLUG).outcome(ME, "pushed", NOW - 10)
    assert not rig.stop().allowed
    rig.ledger.events.append({"by": ME, "at": NOW - 5, "kind": "comment added"})
    assert rig.stop().allowed
    assert rig.ledger.comments == []


def test_another_outcome_after_the_push_lets_the_stop_through(rig):
    rig.commit()
    git(rig.tree, "push", "-u", "origin", BRANCH)
    Progress(rig.store.redis, SLUG).outcome(ME, "pull request opened", NOW - 10)
    assert rig.stop().allowed


def test_a_refused_push_refuses_the_stop(rig):
    rig.ledger.task["pr_url"] = "https://github.com/o/r/pull/7"
    other = rig.root / "other"
    git(rig.seed, "worktree", "add", "-b", "elsewhere", str(other), "origin/dev")
    (other / "theirs").write_text("theirs")
    git(other, "add", "theirs")
    git(other, "commit", "-m", "theirs")
    theirs = git(other, "rev-parse", "HEAD")
    git(other, "push", "origin", f"HEAD:refs/heads/{BRANCH}")
    rig.commit()
    assert rig.stop().reason == TEMPLATE
    assert rig.remote_head() == theirs
    assert rig.ledger.comments == []


def test_a_numbered_second_worktree_in_another_repo_is_pushed_too(rig):
    second = rig.root / "other-repo" / f"{BRANCH}-2"
    git(rig.seed, "worktree", "add", "-b", f"{BRANCH}-2", str(second), "origin/dev")
    rig.ledger.task["pr_url"] = "https://github.com/o/r/pull/7"
    (second / "more").write_text("more")
    git(second, "add", "more")
    git(second, "commit", "-m", "more")
    assert rig.stop().allowed
    assert git(rig.seed, "ls-remote", str(rig.origin), f"refs/heads/{BRANCH}-2")


def test_a_worktree_on_dev_or_main_is_never_pushed(rig):
    rig.ledger.task["pr_url"] = "https://github.com/o/r/pull/7"
    dev = git(rig.seed, "rev-parse", "origin/dev")
    git(rig.tree, "switch", "-c", "main", "origin/dev")
    rig.commit()
    assert rig.stop().allowed
    assert git(rig.seed, "ls-remote", str(rig.origin)).split("\t")[0] == dev
    assert git(rig.seed, "ls-remote", str(rig.origin), "refs/heads/main") == ""
    assert rig.remote_head() == ""


def test_another_agents_worktree_is_left_alone(rig):
    rig.commit()
    (rig.tree / "new").write_text("new\n")
    assert rig.stop(Who(name="engineer@100001-0002", swarm=SLUG, lane="eng", task="t1")).allowed
    assert rig.remote_head() == ""


@pytest.mark.parametrize(
    "who",
    [
        Who(name="master@100001-0003", swarm=SLUG, lane="master", task="t1"),
        Who(name="planner@100001-0004", swarm=SLUG, lane="plan", task="t1"),
        Who(name=ME, swarm="", task="t1"),
        Who(name=ME, swarm=SLUG, lane="eng", task=""),
    ],
)
def test_a_master_a_planner_and_the_operators_sessions_are_never_gated(rig, who):
    rig.ledger.state = lambda slug: {"tasks": [rig.ledger.task], "_meta": {"events": []}}
    own = rig.root / "repo" / plain(who.name)
    if not own.exists():
        git(rig.seed, "worktree", "add", "-b", plain(who.name), str(own), "origin/dev")
    (own / "work").write_text("work")
    git(own, "add", "work")
    git(own, "commit", "-m", "work")
    (own / "new").write_text("new\n")
    assert rig.stop(who).allowed
    assert git(rig.seed, "ls-remote", "--heads", str(rig.origin)).count("refs/heads/") == 1


def test_a_plan_task_is_never_gated(rig):
    rig.ledger.task["kind"] = "plan"
    rig.commit()
    (rig.tree / "new").write_text("new\n")
    assert rig.stop().allowed
    assert rig.remote_head() == ""


def test_the_record_links_branch_and_head_on_github_and_names_them_elsewhere():
    sha = "a" * 40
    for remote in ("git@github.com:o/r.git", "https://github.com/o/r.git", "https://github.com/o/r"):
        assert record_text(remote, "engineer-1", sha) == (
            "The stop hook pushed the task branch https://github.com/o/r/tree/engineer-1 with its head at "
            f"https://github.com/o/r/commit/{sha}"
        )
    assert record_text("/srv/origin.git", "engineer-1", sha) == (
        "The stop hook pushed the task branch engineer-1 to origin"
    )


class DownLedger(FakeLedger):
    def state(self, slug):
        raise SwarmError("ledger demo: ledger server not answering")

    def comment(self, slug, task_id, text, by):
        raise SwarmError("ledger demo: ledger server not answering")


def test_a_ledger_outage_still_pushes_and_refuses_dirty_work(rig):
    rig.gate._ledger = lambda: DownLedger({})
    head = rig.commit()
    assert rig.stop().allowed
    assert rig.remote_head() == head
    assert Progress(rig.store.redis, SLUG).read(ME).outcome == "pushed"
    (rig.tree / "new").write_text("new\n")
    assert rig.stop().reason == TEMPLATE


def test_a_refused_record_keeps_the_push(rig):
    class Refusing(FakeLedger):
        def comment(self, slug, task_id, text, by):
            raise SwarmError("ledger demo refused")

    refusing = Refusing(rig.ledger.task)
    rig.gate._ledger = lambda: refusing
    head = rig.commit()
    assert rig.stop().reason == TEMPLATE
    assert rig.remote_head() == head


def test_a_branch_with_no_work_of_its_own_stops_freely_after_a_pushed_outcome(rig):
    Progress(rig.store.redis, SLUG).outcome(ME, "pushed", NOW - 10)
    assert rig.stop().allowed


def test_a_pushed_outcome_at_zero_is_answered_only_by_a_later_line(rig):
    rig.commit()
    git(rig.tree, "push", "-u", "origin", BRANCH)
    Progress(rig.store.redis, SLUG).outcome(ME, "pushed", 0)
    rig.ledger.events.append({"by": ME, "at": 0, "kind": "comment added"})
    assert not rig.stop().allowed


def test_a_task_the_ledger_no_longer_holds_is_not_gated(rig):
    rig.ledger.task["id"] = "t9"
    rig.commit()
    (rig.tree / "new").write_text("new\n")
    assert rig.stop().allowed
    assert rig.remote_head() == ""


@pytest.mark.parametrize("meta", [None, {}])
def test_a_ledger_with_no_events_counts_as_no_line_since_the_push(rig, meta):
    rig.commit()
    git(rig.tree, "push", "-u", "origin", BRANCH)
    Progress(rig.store.redis, SLUG).outcome(ME, "pushed", NOW - 10)
    doc = {"tasks": [rig.ledger.task]} | ({} if meta is None else {"_meta": meta})
    rig.ledger.state = lambda slug: doc
    assert rig.stop().reason == TEMPLATE


def test_worktrees_sit_under_the_configured_root_or_the_home_default(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKTREE_ROOT", str(tmp_path / "trees"))
    assert PushStop().root() == str(tmp_path / "trees")
    monkeypatch.delenv("WORKTREE_ROOT")
    assert PushStop().root() == Path.home() / "dev" / "worktrees"


def test_the_clock_is_epoch_milliseconds():
    import time

    before = int(time.time() * 1000)
    assert before <= PushStop().now() <= int(time.time() * 1000)


def test_git_runs_in_the_worktree_with_a_timeout(monkeypatch):
    seen = []
    monkeypatch.setattr(push_stop.subprocess, "run", lambda argv, **kw: seen.append((argv, kw)))
    push_stop.git("/w", "status")
    assert seen == [(["git", "-C", "/w", "status"], {"capture_output": True, "text": True, "timeout": 60})]


def test_a_count_git_cannot_answer_is_zero(tmp_path):
    assert count(tmp_path, "HEAD") == 0


def install_gate(rig, code, before=""):
    log = rig.root / "gate.log"
    gate = rig.tree / "scripts" / "ci_prepush"
    gate.mkdir(parents=True)
    (rig.tree / "scripts" / "__init__.py").write_text("")
    (gate / "__init__.py").write_text("")
    (gate / "__main__.py").write_text(
        "import os\n"
        f"with open({str(log)!r}, 'a') as log:\n"
        "    log.write(os.getcwd() + ' ' + os.environ.get('AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN', '') + '\\n')\n"
        "print('gate output')\n"
        "print('gate errors', file=__import__('sys').stderr)\n"
        f"{before}raise SystemExit({code})\n"
    )
    git(rig.tree, "add", "scripts")
    git(rig.tree, "commit", "-m", "gate")
    return log


def test_a_failing_pre_push_gate_keeps_the_branch_off_origin_and_names_the_worktree(monkeypatch, rig):
    monkeypatch.delenv("AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN", raising=False)
    rig.ledger.task["pr_url"] = "https://github.com/o/r/pull/7"
    log = install_gate(rig, 1)
    decision = rig.stop()
    assert (decision.allowed, decision.reason) == (False, f"{TEMPLATE} {GATE_FAILED.format(path=rig.tree)}")
    assert rig.remote_head() == ""
    assert rig.ledger.comments == []
    assert log.read_text() == f"{rig.tree} \n"


def test_a_passing_pre_push_gate_runs_in_the_worktree_with_the_local_test_setting_then_pushes(capfd, monkeypatch, rig):
    monkeypatch.setenv("AGENTIHOOKS_ALLOW_LOCAL_TEST_RUN", "true")
    rig.ledger.task["pr_url"] = "https://github.com/o/r/pull/7"
    log = install_gate(rig, 0)
    capfd.readouterr()
    assert rig.stop().allowed
    assert rig.remote_head() == git(rig.tree, "rev-parse", "HEAD")
    assert log.read_text() == f"{rig.tree} true\n"
    assert capfd.readouterr() == ("", "")


def test_a_head_the_gate_already_passed_is_pushed_without_running_it_again(rig):
    from scripts.ci_prepush import stamp_path

    rig.ledger.task["pr_url"] = "https://github.com/o/r/pull/7"
    log = install_gate(rig, 1)
    head = git(rig.tree, "rev-parse", "HEAD")
    stamp_path(rig.tree).write_text(head)
    assert rig.stop().allowed
    assert rig.remote_head() == head
    assert not log.exists()


def test_the_failure_text_is_exact():
    assert GATE_FAILED.format(path="/w") == (
        "The pre push gate failed in /w, so the stop hook did not push it. "
        "Run python -m scripts.ci_prepush there, fix what fails and commit."
    )
    assert GATE_SLOW.format(path="/w", seconds=8.0) == (
        "The pre push gate did not finish in 8 s in /w, so the stop hook did not push it. "
        "Run python -m scripts.ci_prepush there and commit."
    )


def gone(pid):
    status = Path(f"/proc/{pid}/status")
    for _ in range(40):
        if not status.exists() or "State:\tZ" in status.read_text():
            return True
        time.sleep(0.05)
    return False


def test_a_pre_push_gate_past_its_timeout_is_killed_with_its_children_and_keeps_the_branch_off_origin(monkeypatch, rig):
    rig.ledger.task["pr_url"] = "https://github.com/o/r/pull/7"
    pid = rig.root / "child.pid"
    slow = (
        "import subprocess, time\n"
        "import sys\n"
        "stubborn = 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)'\n"
        "child = subprocess.Popen([sys.executable, '-c', stubborn])\n"
        f"open({str(pid)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(30)\n"
    )
    install_gate(rig, 0, before=slow)
    monkeypatch.setattr(push_stop, "CONDITIONS_TIMEOUT_SEC", RESERVE_S + 2.26)
    rig.ticks[:] = [0.0]
    decision = rig.stop(started=0.0)
    assert (decision.allowed, decision.reason) == (False, f"{TEMPLATE} {GATE_SLOW.format(path=rig.tree, seconds=2.3)}")
    assert rig.remote_head() == ""
    assert gone(int(pid.read_text()))


def test_the_reserve_holds_the_measured_push_and_ledger_write_and_the_floor_is_one_second():
    assert (RESERVE_S, FLOOR_S) == (3.5, 1.0)


def test_the_late_text_is_exact():
    assert GATE_LATE.format(path="/w") == (
        "The stop hook had too little time left to run the pre push gate in /w, so it did not push it. "
        "Run python -m scripts.ci_prepush there and commit."
    )


def second_tree_with_work(rig):
    second = rig.root / "other-repo" / f"{BRANCH}-2"
    git(rig.seed, "worktree", "add", "-b", f"{BRANCH}-2", str(second), "origin/dev")
    (second / "more").write_text("more")
    git(second, "add", "more")
    git(second, "commit", "-m", "more")
    return second


def slow_origin(rig, seconds):
    hook = rig.origin / "hooks" / "pre-receive"
    hook.write_text(f"#!/bin/sh\nsleep {seconds}\n")
    hook.chmod(0o755)


def test_under_a_slow_push_a_later_gate_is_killed_with_the_reserve_left_inside_the_stop_condition(monkeypatch, rig):
    rig.ledger.task["pr_url"] = "https://github.com/o/r/pull/7"
    second_tree_with_work(rig)
    log = install_gate(rig, 0, before="import time\ntime.sleep(30)\n")
    slow_origin(rig, 2)
    timeout = RESERVE_S + FLOOR_S + 4
    monkeypatch.setattr(push_stop, "CONDITIONS_TIMEOUT_SEC", timeout)
    started = time.monotonic()
    decision = rig.stop()
    took = time.monotonic() - started
    assert git(rig.seed, "ls-remote", str(rig.origin), f"refs/heads/{BRANCH}-2")
    assert rig.remote_head() == ""
    assert log.read_text() == f"{rig.tree} \n"
    slow = re.fullmatch(
        re.escape(f"{TEMPLATE} The pre push gate did not finish in ")
        + r"(\d+(?:\.\d)?)"
        + re.escape(
            f" s in {rig.tree}, so the stop hook did not push it. Run python -m scripts.ci_prepush there and commit."
        ),
        decision.reason,
    )
    assert slow
    assert FLOOR_S <= float(slow.group(1)) <= timeout - RESERVE_S - 2
    assert 2 <= took <= timeout - RESERVE_S + 0.5


def test_under_a_slow_push_a_later_gate_with_less_than_the_floor_left_never_starts(monkeypatch, rig):
    rig.ledger.task["pr_url"] = "https://github.com/o/r/pull/7"
    second_tree_with_work(rig)
    log = install_gate(rig, 0)
    slow_origin(rig, 1)
    timeout = RESERVE_S + FLOOR_S + 0.5
    monkeypatch.setattr(push_stop, "CONDITIONS_TIMEOUT_SEC", timeout)
    started = time.monotonic()
    decision = rig.stop()
    took = time.monotonic() - started
    assert git(rig.seed, "ls-remote", str(rig.origin), f"refs/heads/{BRANCH}-2")
    assert (decision.allowed, decision.reason) == (False, f"{TEMPLATE} {GATE_LATE.format(path=rig.tree)}")
    assert rig.remote_head() == ""
    assert not log.exists()
    assert 1 <= took <= timeout


def test_a_gate_left_exactly_the_floor_is_started_with_that_budget(monkeypatch, rig):
    install_gate(rig, 0)
    waits = []
    monkeypatch.setattr(
        push_stop.subprocess, "Popen", lambda *a, **kw: type("Gate", (), {"wait": lambda _, s: waits.append(s) or 0})()
    )
    tree = push_stop.Tree(rig.tree, BRANCH, False, 1, 1)
    assert push_stop.gate_refusal(tree, lambda: FLOOR_S) is None
    assert waits == [FLOOR_S]
    assert push_stop.gate_refusal(tree, lambda: FLOOR_S - 0.01) == GATE_LATE.format(path=rig.tree)
    assert waits == [FLOOR_S]


def test_a_gate_left_just_under_the_floor_never_starts(monkeypatch, rig):
    rig.ledger.task["pr_url"] = "https://github.com/o/r/pull/7"
    log = install_gate(rig, 0)
    monkeypatch.setattr(push_stop, "CONDITIONS_TIMEOUT_SEC", RESERVE_S + FLOOR_S - 0.01)
    rig.ticks[:] = [0.0]
    decision = rig.stop(started=0.0)
    assert (decision.allowed, decision.reason) == (False, f"{TEMPLATE} {GATE_LATE.format(path=rig.tree)}")
    assert rig.remote_head() == ""
    assert not log.exists()


def test_the_gate_budget_counts_from_when_the_hook_started_not_from_when_the_gate_starts(monkeypatch, rig):
    rig.ledger.task["pr_url"] = "https://github.com/o/r/pull/7"
    log = install_gate(rig, 0)
    monkeypatch.setattr(push_stop, "CONDITIONS_TIMEOUT_SEC", RESERVE_S + FLOOR_S + 4)
    rig.ticks[:] = [4.5]
    decision = rig.stop(started=0.0)
    assert (decision.allowed, decision.reason) == (False, f"{TEMPLATE} {GATE_LATE.format(path=rig.tree)}")
    assert not log.exists()


def test_the_hook_clock_is_the_injected_clock_or_the_monotonic_clock():
    assert PushStop(clock=lambda: 42.5).clock() == 42.5
    assert abs(PushStop().clock() - time.monotonic()) < 1


def test_the_hook_start_is_read_when_the_gate_is_built():
    assert PushStop(clock=lambda: 7.0).started == 7.0


def test_a_passing_gate_followed_by_a_slow_push_and_ledger_write_ends_inside_the_stop_condition(monkeypatch, rig):
    rig.ledger.task["pr_url"] = "https://github.com/o/r/pull/7"
    log = install_gate(rig, 0, before="import time\ntime.sleep(1.5)\n")
    slow_origin(rig, 2.3)
    rig.ledger.delay = 0.52
    timeout = RESERVE_S + 2
    monkeypatch.setattr(push_stop, "CONDITIONS_TIMEOUT_SEC", timeout)
    started = time.monotonic()
    assert rig.stop(started=started).allowed
    took = time.monotonic() - started
    assert log.exists()
    assert rig.remote_head() == git(rig.tree, "rev-parse", "HEAD")
    assert len(rig.ledger.comments) == 1
    assert 1.5 + 2.3 + 0.52 <= took <= timeout


def test_a_pre_push_gate_killed_by_a_signal_keeps_the_branch_off_origin(rig):
    rig.ledger.task["pr_url"] = "https://github.com/o/r/pull/7"
    install_gate(rig, "os.kill(os.getpid(), 9)")
    decision = rig.stop()
    assert (decision.allowed, decision.reason) == (False, f"{TEMPLATE} {GATE_FAILED.format(path=rig.tree)}")
    assert rig.remote_head() == ""


def test_a_pushed_head_with_dirty_files_never_runs_the_gate(rig):
    rig.ledger.task["pr_url"] = "https://github.com/o/r/pull/7"
    log = install_gate(rig, 1)
    git(rig.tree, "push", "origin", f"HEAD:refs/heads/{BRANCH}")
    (rig.tree / "readme").write_text("changed\n")
    decision = rig.stop()
    assert (decision.allowed, decision.reason) == (False, TEMPLATE)
    assert not log.exists()
