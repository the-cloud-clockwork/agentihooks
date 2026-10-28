import io
import json
from types import SimpleNamespace
from unittest.mock import patch

import fakeredis
import pytest

from scripts.gates import entry, lift
from scripts.gates.subagents import SubagentBudget
from scripts.swarm import cli
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

SLUG = "shared-lifts"
AGENT = "engineer@1-1"
TASK = "t1"
URL = "https://github.com/o/r/pull/7"


@pytest.fixture
def hosts(tmp_path, monkeypatch):
    server = fakeredis.FakeServer()
    master = RedisStore(fakeredis.FakeRedis(server=server, decode_responses=True))
    hive = RedisStore(fakeredis.FakeRedis(server=server, decode_responses=True))
    master.create(SwarmConfig(SLUG, "/r", 1, 0))
    master.put_agent(SLUG, AgentRecord(name=AGENT, lane="eng", task=TASK))
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "operator")
    monkeypatch.setattr(cli, "LedgerClient", lambda: SimpleNamespace(task=lambda *args: {"pr_url": URL}))
    monkeypatch.setattr("scripts.gates.intent.pr_head", lambda url: "a1")
    monkeypatch.setattr("scripts.gates.intent.production_unchanged", lambda *args: False)
    return master, hive, tmp_path / "master", tmp_path / "hive"


def check(home, agent=AGENT, task=TASK):
    payload = {"tool_name": "Agent", "tool_input": {"subagent_type": "Explore", "description": "read", "prompt": "go"}}
    env = {"AGENTIHOOKS_SWARM": SLUG, "AGENTIHOOKS_AGENT_NAME": agent, "AGENTIHOOKS_SWARM_TASK": task}
    with patch("sys.stderr", io.StringIO()), patch.dict(entry.GATES, {"subagents": SubagentBudget(launches=2)}):
        return entry.main(["subagents"], io.StringIO(json.dumps(payload)), env, home)


def exhaust(home, agent=AGENT, task=TASK):
    assert check(home, agent, task) == 0
    assert check(home, agent, task) == 0
    assert check(home, agent, task) == 2


def test_master_lift_reaches_another_host_and_expires_on_new_head(hosts, monkeypatch):
    master, hive, master_home, hive_home = hosts
    exhaust(hive_home)
    with (
        patch("scripts.swarm.store.connect", return_value=master),
        patch("scripts.gates.log.gates_dir", side_effect=lambda slug, home=None: master_home / slug / "gates"),
    ):
        cli.cmd_lift(master, SimpleNamespace(slug=SLUG, agent=AGENT, gate="subagents"))
    with patch("scripts.swarm.store.connect", return_value=hive):
        assert check(hive_home) == 0
        assert check(hive_home, agent="engineer@1-2") == 0
        monkeypatch.setattr("scripts.gates.intent.pr_head", lambda url: "b2")
        assert check(hive_home) == 2
        monkeypatch.setattr("scripts.gates.intent.pr_head", lambda url: None)
        assert check(hive_home) == 2


def test_unlifted_task_still_refused_and_revocation_reaches_hive(hosts):
    master, hive, master_home, hive_home = hosts
    exhaust(hive_home)
    exhaust(hive_home, agent="engineer@2-1", task="t2")
    with patch("scripts.swarm.store.connect", return_value=master):
        lift.lift_agent(
            entry.Who(name=AGENT, swarm=SLUG, task=TASK), "subagents", master_home, pull={"url": URL, "head": "a1"}
        )
    with patch("scripts.swarm.store.connect", return_value=hive):
        assert check(hive_home) == 0
        assert check(hive_home, agent="engineer@2-1", task="t2") == 2
    with patch("scripts.swarm.store.connect", return_value=master):
        lift.drop_task(SLUG, TASK, master_home)
    with patch("scripts.swarm.store.connect", return_value=hive):
        assert check(hive_home) == 2


def test_agent_lift_without_task_reaches_hive_and_keeps_time_limit(hosts):
    master, hive, master_home, hive_home = hosts
    with patch("scripts.swarm.store.connect", return_value=master):
        lift.lift_agent(entry.Who(name=AGENT, swarm=SLUG), "talk", master_home, now=100)
    with patch("scripts.swarm.store.connect", return_value=hive):
        assert lift.agent_lifted(SLUG, AGENT, "talk", hive_home, now=101)
        assert not lift.agent_lifted(SLUG, "engineer@2-1", "talk", hive_home, now=101)
        assert not lift.agent_lifted(SLUG, AGENT, "talk", hive_home, now=100 + lift.LIFT_SECONDS)
