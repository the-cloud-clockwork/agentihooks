import copy

import pytest

from scripts.doctor import spawns
from tests.doctor.recorded import load

pytestmark = pytest.mark.xdist_group("fakeredis")


from tests.swarm.profile_fixture import validated


def test_recorded_spawns_and_planted_failed_spawn():
    record = load("spawns")
    assert spawns.failed(record) == []
    planted = copy.deepcopy(record)
    planted["actions"].append(f"{record['slug']}: spawn failed for dt2: init-agent timed out")
    [found] = spawns.failed(planted)
    assert found.id == "failed-spawn/dt2"
    assert found.measure == 1
    assert "init-agent timed out" in found.evidence[0]
    assert record["actions"] == []


def test_runtime_records_the_launcher_overflow_placement(tmp_path):
    import subprocess

    import fakeredis

    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm.store import RedisStore, SwarmConfig
    from scripts.swarm.tick import tick

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    config = SwarmConfig("sw", str(tmp_path), max_eng=1, max_ci=0, state="running")
    store.create(config)
    launched = []

    def run(argv, **kwargs):
        launched.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            validated(
                argv,
                "status=started\nroute_status=routed\nagent=codex\naccount=default\nplacement=overflow\npane_id=1\n",
            ),
            "",
        )

    runtime = HerdrRuntime(
        home=tmp_path, run=run, choose=lambda *args: ("codex", "fallthrough: claude is at its session cap")
    )
    runtime.live_names = lambda: set()
    runtime.conversations = lambda: {}
    runtime.has_capacity = lambda config: True
    runtime.quota_capacity = None

    class Ledger:
        def state(self, slug):
            return {"tasks": self.tasks(slug)}

        def tasks(self, slug):
            return [
                {
                    "id": "task",
                    "title": "Task",
                    "description": "",
                    "lane": "eng",
                    "profile": "engineer",
                    "state": "open",
                    "depends_on": [],
                    "territory": [],
                }
            ]

        def update_task(self, slug, task_id, fields, **kwargs):
            return {"state": "open", "claimed_by": "", **fields}

        def notify(self, *args):
            pass

    actions = tick("sw", store, Ledger(), runtime, 1000)
    agents = store.agents("sw")
    assert len(agents) == 2, actions
    assert all(a.placement == "overflow" for a in agents)
    assert [a.choice for a in agents if a.lane == "eng"] == ["overflow"]


def test_recorded_restores_and_planted_fresh_fallback():
    record = load("spawns")
    assert spawns.fresh_restores(record) == []
    planted = copy.deepcopy(record)
    planted["restored"] = [
        {
            "name": "agent",
            "task": "dt2",
            "lane": "eng",
            "outcome": "fresh",
            "reason": "conversation is unavailable",
            "conversation_id": "prior",
            "at": 1000,
        }
    ]
    [found] = spawns.fresh_restores(planted)
    assert found.id == "fresh-restore/agent"
    assert found.measure == 1
    assert "conversation is unavailable" in found.evidence
    planted["restored"][0]["outcome"] = "resumed"
    assert spawns.fresh_restores(planted) == []


def test_recorded_agents_with_no_hook_event_one_tick_after_spawn_are_flagged():
    agents = load("spawns")["agents"]
    last = max(a["started_at"] for a in agents)
    found = spawns.silent_starts(agents, {}, last + spawns.TICK_MS)
    assert [f.id for f in found] == sorted(f"no-hook-event/{a['name']}" for a in agents)
    assert all(f.measure >= 1 for f in found)
    assert spawns.silent_starts(agents, {a["name"]: a["started_at"] + 1 for a in agents}, last + spawns.TICK_MS) == []
    assert spawns.silent_starts(agents, {}, last + spawns.TICK_MS - 1) == [
        f for f in found if f.subject != max(agents, key=lambda a: a["started_at"])["name"]
    ]
    finished = [{**a, "state": "finished"} for a in agents]
    assert spawns.silent_starts(finished, {}, last + spawns.TICK_MS) == []
