import re
import subprocess
from pathlib import Path

import pytest

from scripts.handoff.envelope import build as envelope
from scripts.swarm import prompt
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.tick import STARTUP_GRACE_MS, tick
from tests.test_worktree_skill import WtBase

pytestmark = pytest.mark.xdist_group("fakeredis")

PREDECESSOR = "engineer@a1b2c3-0001"
SUCCESSOR = "engineer@a1b2c3-0002"
CONTINUED = {
    "branch": "engineer-a1b2c3-0001",
    "remote_branch": "engineer-a1b2c3-0001",
    "remote_head": "4f2a9c0",
    "continue_from": "origin/engineer-a1b2c3-0001",
    "fresh_reason": "none",
}
FRESH = {**CONTINUED, "remote_head": "none", "continue_from": "fresh"}
FRESH["fresh_reason"] = "branch engineer-a1b2c3-0001 is not on the remote"


def _task(envelope=None):
    task = {"id": "t1", "title": "task", "description": "task", "seat": "eng-1@sw", "handoff": "document"}
    return {**task, "handoff_envelope": envelope} if envelope else task


def test_a_successor_cuts_its_worktree_from_the_predecessors_branch_and_pushes_back_to_it():
    rendered = prompt.build("sw", "/repo", "eng", SUCCESSOR, _task(CONTINUED))
    assert "wt.sh new engineer-a1b2c3-0002 --from origin/engineer-a1b2c3-0001 " in rendered
    assert "git push -u origin HEAD:engineer-a1b2c3-0001" in rendered
    assert "wt.sh new engineer-a1b2c3-0002 (" not in rendered


def test_a_successor_whose_branch_is_gone_starts_fresh_and_reads_why():
    rendered = prompt.build("sw", "/repo", "eng", SUCCESSOR, _task(FRESH))
    assert "branch engineer-a1b2c3-0001 is not on the remote" in rendered
    assert "--from" not in rendered
    assert "wt.sh new engineer-a1b2c3-0002 (" in rendered


@pytest.mark.parametrize("kind", ["troubleshoot", "tune", "ops"])
def test_every_kind_that_cuts_a_worktree_continues_the_branch(kind):
    rendered = prompt.build("sw", "/repo", "eng", SUCCESSOR, {**_task(CONTINUED), "kind": kind})
    assert "wt.sh new engineer-a1b2c3-0002 --from origin/engineer-a1b2c3-0001" in rendered


def test_a_first_life_has_no_branch_to_continue():
    assert "--from" not in prompt.build("sw", "/repo", "eng", SUCCESSOR, _task())


@pytest.mark.parametrize("envelope", [None, {"reason": "quota"}])
def test_no_continuation_line_without_a_branch_verdict(envelope):
    rendered = prompt.build("sw", "/repo", "eng", SUCCESSOR, _task(envelope))
    assert "--from" not in rendered and "Start your worktree" not in rendered and "continue it" not in rendered


def test_the_continuation_line_names_the_branch_its_head_and_the_push():
    assert prompt.continuation_lines(_task(CONTINUED)) == [
        "Your predecessor's branch engineer-a1b2c3-0001 is on the remote at 4f2a9c0: continue it. The worktree "
        "step below cuts your worktree from it; push with git push -u origin HEAD:engineer-a1b2c3-0001 so its "
        "commits and its pull request carry on."
    ]


def test_the_fresh_line_carries_the_recorded_reason():
    assert prompt.continuation_lines(_task(FRESH)) == [
        "Start your worktree fresh from dev: branch engineer-a1b2c3-0001 is not on the remote."
    ]


def test_the_prompt_places_the_continuation_line_before_the_seat_history():
    rendered = prompt.build("sw", "/repo", "eng", SUCCESSOR, _task(CONTINUED))
    assert rendered.index("continue it. The worktree") < rendered.index("carries what earlier occupants left")


def _reclaimed(verdict):
    task = {key: value for key, value in _task().items() if key != "handoff"}
    return {**task, "reclaim": {key: verdict[key] for key in RECLAIM_KEYS}}


RECLAIM_KEYS = ("remote_branch", "remote_head", "continue_from", "fresh_reason")


def test_a_reclaimed_task_cuts_its_worktree_from_the_retired_agents_branch():
    rendered = prompt.build("sw", "/repo", "eng", SUCCESSOR, _reclaimed(CONTINUED))
    assert "wt.sh new engineer-a1b2c3-0002 --from origin/engineer-a1b2c3-0001 " in rendered
    assert "git push -u origin HEAD:engineer-a1b2c3-0001" in rendered


def test_a_reclaimed_task_with_no_pushed_branch_starts_fresh_and_reads_why():
    rendered = prompt.build("sw", "/repo", "eng", SUCCESSOR, _reclaimed(FRESH))
    assert "Start your worktree fresh from dev: branch engineer-a1b2c3-0001 is not on the remote." in rendered
    assert "--from" not in rendered


def test_a_handoff_envelope_wins_over_a_reclaim():
    rendered = prompt.build("sw", "/repo", "eng", SUCCESSOR, {**_task(CONTINUED), "reclaim": FRESH})
    assert "--from origin/engineer-a1b2c3-0001" in rendered and "Start your worktree fresh" not in rendered


class TestReclaimAfterARetirement(WtBase):
    def test_a_reclaim_after_a_retirement_with_a_pushed_commit_starts_on_that_commit(self):
        store, ledger, runtime, first = self._first_life()
        made = self.run_wt("new", first.replace("@", "-"))
        self.assertEqual(made.returncode, 0, made.stderr)
        mine = made.stdout.strip().splitlines()[-1]
        Path(mine, "work.txt").write_text("done\n")
        subprocess.run(["git", "-C", mine, "add", "work.txt"], check=True, env=self.gitenv)
        subprocess.run(["git", "-C", mine, "commit", "-qm", "finished work"], check=True, env=self.gitenv)
        subprocess.run(["git", "-C", mine, "push", "-qu", "origin", "HEAD"], check=True, env=self.gitenv)

        rendered = self._reclaim(store, ledger, runtime, first)
        self.assertIn(f"--from origin/{first.replace('@', '-')}", rendered)
        self.assertEqual(self._head(self._cut(rendered)), self._head(mine))

    def test_a_reclaim_after_a_retirement_that_never_pushed_starts_fresh_and_records_why(self):
        store, ledger, runtime, first = self._first_life()
        rendered = self._reclaim(store, ledger, runtime, first)
        why = f"no earlier life pushed a branch: checked {first.replace('@', '-')} on the remote"
        self.assertIn(f"Start your worktree fresh from dev: {why}.", rendered)
        self.assertEqual(store.reclaims("sw")[runtime.spawned[-1][1]]["fresh_reason"], why)
        dev = subprocess.run(
            ["git", "-C", str(self.primary), "rev-parse", "origin/dev"], capture_output=True, text=True
        )
        self.assertEqual(self._head(self._cut(rendered)), dev.stdout.strip())

    def _first_life(self):
        import fakeredis

        from tests.swarm.test_tick import FakeRuntime, tasks

        store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
        store.create(SwarmConfig("sw", str(self.primary), 1, 1))
        ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
        tick("sw", store, ledger, runtime, now_ms=1_000)
        return store, ledger, runtime, runtime.spawned[-1][1]

    def _reclaim(self, store, ledger, runtime, first):
        runtime.live.discard(first)
        tick("sw", store, ledger, runtime, now_ms=2_000 + STARTUP_GRACE_MS)
        successor = runtime.spawned[-1][1]
        self.assertNotEqual(successor, first)
        task = {"title": "task", "description": "task", **runtime.tasks[-1]}
        return prompt.build("sw", str(self.primary), "eng", successor, task)

    def _cut(self, rendered):
        plain, rest = re.search(r"wt\.sh new (\S+)( --from \S+)?", rendered).groups()
        cut = self.run_wt("new", plain, *(rest.split() if rest else []))
        self.assertEqual(cut.returncode, 0, cut.stderr)
        return cut.stdout.strip().splitlines()[-1]

    def _head(self, worktree):
        return subprocess.run(
            ["git", "-C", worktree, "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()


class TestHandoffOnAPushedBranch(WtBase):
    def test_the_successor_worktree_head_equals_the_predecessor_head(self):
        import fakeredis

        made = self.run_wt("new", "engineer-a1b2c3-0001")
        self.assertEqual(made.returncode, 0, made.stderr)
        mine = made.stdout.strip().splitlines()[-1]
        git = ["git", "-C", mine]
        (self.worktree_root / "primary" / "engineer-a1b2c3-0001" / "work.txt").write_text("done\n")
        subprocess.run([*git, "add", "work.txt"], check=True, env=self.gitenv)
        subprocess.run([*git, "commit", "-qm", "finished work"], check=True, env=self.gitenv)
        subprocess.run([*git, "push", "-qu", "origin", "HEAD"], check=True, env=self.gitenv)
        head = subprocess.run([*git, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()

        store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
        store.create(SwarmConfig("sw", str(self.primary), 1, 1))
        agent = AgentRecord(PREDECESSOR, "eng", "t1", seat="eng-1@sw")
        handed = envelope(store, "sw", agent, "recycle", [{"id": "t1", "phase": "p1"}], 0)
        self.assertEqual((handed["remote_head"], handed["continue_from"]), (head, "origin/engineer-a1b2c3-0001"))

        theirs = self._successor(SUCCESSOR, handed)
        self.assertEqual(self._head(theirs), head)

        subprocess.run(
            ["git", "-C", theirs, "commit", "-q", "--allow-empty", "-m", "more"], check=True, env=self.gitenv
        )
        push = re.search(r"git push -u origin (\S+) so", prompt.build("sw", "/r", "eng", SUCCESSOR, _task(handed)))
        subprocess.run(["git", "-C", theirs, "push", "-qu", "origin", push.group(1)], check=True, env=self.gitenv)
        again = envelope(store, "sw", AgentRecord(SUCCESSOR, "eng", "t1", seat="eng-1@sw"), "recycle", [], 0)
        self.assertEqual((again["remote_branch"], again["remote_head"]), ("engineer-a1b2c3-0001", self._head(theirs)))
        third = self._successor("engineer@a1b2c3-0003", again)
        self.assertEqual(self._head(third), self._head(theirs))

    def _successor(self, name, handed):
        rendered = prompt.build("sw", str(self.primary), "eng", name, _task(handed))
        plain, ref = re.search(r"wt\.sh new (\S+) --from (\S+)", rendered).groups()
        cut = self.run_wt("new", plain, "--from", ref)
        self.assertEqual(cut.returncode, 0, cut.stderr)
        return cut.stdout.strip().splitlines()[-1]

    def _head(self, worktree):
        return subprocess.run(
            ["git", "-C", worktree, "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
