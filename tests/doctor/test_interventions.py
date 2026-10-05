import os
import subprocess
from argparse import Namespace

import pytest

from scripts.doctor import interventions
from scripts.inbox.store import InboxStore
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError

pytestmark = pytest.mark.xdist_group("fakeredis")
WATCHED, DOCTOR = "watch", "watch-doctor"
ENGINEER = f"{WATCHED}-eng-1"


class FakeRun:
    def __init__(self, outputs=None, failing=()):
        self.calls, self.outputs, self.failing = [], outputs or {}, failing

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        code = 1 if any(word in argv for word in self.failing) else 0
        out = next((text for word, text in self.outputs.items() if word in argv), "")
        return subprocess.CompletedProcess(argv, code, out, "refused" if code else "")


class FakeLedger:
    def __init__(self):
        self.said = []

    def say(self, slug, text, by=None):
        self.said.append((slug, text, by))


@pytest.fixture
def store():
    import fakeredis

    found = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    found.create(SwarmConfig(WATCHED, "/repo", 2, 1))
    found.create(SwarmConfig(DOCTOR, "/repo", 1, 0, template="doctor"))
    found.put_agent(WATCHED, AgentRecord(ENGINEER, "eng", "t1", seat=f"eng-1@{WATCHED}"))
    return found


def context(store, run=None, **paths):
    return interventions.Context(
        store, FakeLedger(), WATCHED, DOCTOR, "doctor", run or FakeRun({"rev-parse": "dev\n"}), **paths
    )


def args(to="", text="", file=""):
    return Namespace(to=to, text=text, file=file)


def logged_on_both(ctx):
    return sorted(slug for slug, _, _ in ctx.ledger.said) == sorted([WATCHED, DOCTOR])


@pytest.mark.parametrize("action", ["task-set", "task-reopen", "retire", "kill", "terminate-agent", "release", "close"])
def test_the_allow_list_refuses_every_other_action_and_touches_nothing(store, action):
    ctx = context(store)
    with pytest.raises(SwarmError, match="not an allowed intervention") as refused:
        interventions.apply(ctx, action, args(to=ENGINEER, text="x"))
    assert "tasks" in str(refused.value) and "kills" in str(refused.value)
    assert ctx.run.calls == [] and ctx.ledger.said == []
    assert InboxStore(store.redis).inbox(f"eng-1@{WATCHED}") == []
    assert [a.name for a in store.agents(WATCHED)] == [ENGINEER]


def test_pull_dev_fast_forwards_the_main_checkout_and_logs_on_both_ledgers(store):
    ctx = context(store)
    interventions.apply(ctx, "pull-dev", args())
    assert ctx.run.calls[-1] == ["git", "-C", "/repo", "pull", "--ff-only", "origin", "dev"]
    assert logged_on_both(ctx)


def test_pull_dev_refuses_a_checkout_that_is_not_on_dev(store):
    ctx = context(store, FakeRun({"rev-parse": "feature\n"}))
    with pytest.raises(SwarmError, match="not dev"):
        interventions.apply(ctx, "pull-dev", args())
    assert not any("pull" in call for call in ctx.run.calls) and ctx.ledger.said == []


def test_a_failed_command_is_reported_and_not_logged(store):
    ctx = context(store, FakeRun({"rev-parse": "dev\n"}, failing=("pull",)))
    with pytest.raises(SwarmError, match="refused"):
        interventions.apply(ctx, "pull-dev", args())
    assert ctx.ledger.said == []


def test_the_ledger_server_restarts_only_when_its_code_changed(store, tmp_path):
    code, pidfile = tmp_path / "code", tmp_path / ".server.pid"
    code.mkdir()
    (code / "ledger_server.py").write_text("x")
    pidfile.write_text("1")
    os.utime(code / "ledger_server.py", (100, 100))
    os.utime(pidfile, (200, 200))
    ctx = context(store, code=code, pidfile=pidfile)
    with pytest.raises(SwarmError, match="current code"):
        interventions.apply(ctx, "restart-ledger-server", args())
    assert ctx.run.calls == []
    os.utime(code / "ledger_server.py", (300, 300))
    interventions.apply(ctx, "restart-ledger-server", args())
    assert [call[-1] for call in ctx.run.calls] == ["--stop", "--ensure"]
    assert logged_on_both(ctx)


def test_refresh_rules_runs_the_rules_refresh(store):
    ctx = context(store)
    interventions.apply(ctx, "refresh-rules", args())
    assert ctx.run.calls == [["agentihooks", "refresh-rules"]] and logged_on_both(ctx)


def test_culture_replaces_the_watched_swarm_culture(store, tmp_path):
    (tmp_path / "culture.md").write_text("Prove every fix.\n")
    ctx = context(store)
    interventions.apply(ctx, "culture", args(file=str(tmp_path / "culture.md")))
    assert store.culture.get(WATCHED) == "Prove every fix.\n" and logged_on_both(ctx)


def test_handoff_at_stop_asks_a_live_agent_of_the_watched_swarm(store):
    ctx = context(store)
    interventions.apply(ctx, "handoff-at-stop", args(to=ENGINEER))
    [item] = InboxStore(store.redis).inbox(f"eng-1@{WATCHED}")
    assert item.sender == f"master@{DOCTOR}" and "next stop" in item.text and f"swarm {WATCHED} handoff" in item.text
    assert logged_on_both(ctx)
    with pytest.raises(SwarmError, match="no agent"):
        interventions.apply(ctx, "handoff-at-stop", args(to=f"{WATCHED}-eng-9"))


def test_message_reaches_the_watched_master_or_an_agent_and_nobody_else(store):
    ctx = context(store)
    interventions.apply(ctx, "message", args(to=f"master@{WATCHED}", text="The fix is merged."))
    [item] = InboxStore(store.redis).inbox(f"master@{WATCHED}")
    assert item.sender == f"master@{DOCTOR}" and item.text == "The fix is merged."
    interventions.apply(ctx, "message", args(to=ENGINEER, text="Pull dev before your next commit."))
    assert len(ctx.ledger.said) == 4
    for outside in ("master@other", "operator", "other-eng-1"):
        with pytest.raises(SwarmError, match="watched swarm"):
            interventions.apply(ctx, "message", args(to=outside, text="x"))
    with pytest.raises(SwarmError, match="text"):
        interventions.apply(ctx, "message", args(to=f"master@{WATCHED}"))
