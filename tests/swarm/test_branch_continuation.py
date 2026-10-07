import re
import subprocess

import pytest

from scripts.handoff.envelope import build as envelope
from scripts.swarm import prompt
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
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

        rendered = prompt.build("sw", str(self.primary), "eng", SUCCESSOR, _task(handed))
        name, ref = re.search(r"wt\.sh new (\S+) --from (\S+)", rendered).groups()
        cut = self.run_wt("new", name, "--from", ref)
        self.assertEqual(cut.returncode, 0, cut.stderr)
        theirs = cut.stdout.strip().splitlines()[-1]
        successor = subprocess.run(["git", "-C", theirs, "rev-parse", "HEAD"], capture_output=True, text=True)
        self.assertEqual(successor.stdout.strip(), head)
