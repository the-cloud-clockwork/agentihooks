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


def _picks(prefix, harnesses, at, choice="share"):
    return [{"name": f"{prefix}{i}", "harness": h, "started_at": at, "choice": choice} for i, h in enumerate(harnesses)]


def _windowed(agents, history=()):
    return {
        "slug": "sw",
        "target": 20,
        "target_changed_at": 1,
        "now": 2 * spawns.WINDOW_MS,
        "spawns": {"codex": 77, "claude": 250},
        "agents": list(agents),
        "history": list(history),
    }


def test_recorded_share_and_planted_drift():
    record = load("spawns")
    record.update(now=max(a["started_at"] for a in record["agents"]), history=[])
    assert spawns.share_drift(record) == []
    now = 2 * spawns.WINDOW_MS
    planted = _windowed(_picks("a", ["claude"] * 10, now - 1))
    [found] = spawns.share_drift(planted)
    assert found.id == "codex-share-drift/sw"
    assert found.measure == 20
    assert "codex 0/10 share picks in the last 6 hours, 0.0%, target 20%" in found.evidence


def test_overflow_forced_and_old_spawns_do_not_count_toward_drift():
    now = 2 * spawns.WINDOW_MS
    on_target = _picks("a", ["claude"] * 8 + ["codex"] * 2, now - 1)
    overflow = _picks("o", ["codex"] * 6, now - 1, choice="overflow")
    forced = _picks("f", ["claude"] * 6, now - 1, choice="forced")
    old = _picks("h", ["codex"] * 6, now - spawns.WINDOW_MS - 1)
    unrecorded = [{"name": "u", "harness": "codex", "started_at": now - 1}]
    assert spawns.share_drift(_windowed([*on_target, *forced, *unrecorded], [*overflow, *old])) == []
    [found] = spawns.share_drift(_windowed(on_target, _picks("h", ["codex"] * 6, now - 1)))
    assert found.evidence == ("codex 8/16 share picks in the last 6 hours, 50.0%, target 20%",)


def test_the_window_starts_inclusive_and_a_pick_without_a_start_is_outside_it():
    now = spawns.WINDOW_MS + 2
    edge = _picks("e", ["claude"] * 10, 2)
    undated = [{"name": "u", "harness": "codex", "choice": "share"}]
    [found] = spawns.share_drift(_windowed(edge, undated) | {"now": now})
    assert found.evidence == ("codex 0/10 share picks in the last 6 hours, 0.0%, target 20%",)
    assert found.threshold == "more than one share pick from target"
    assert found.summary == "Codex share 0.0% against target 20%"


def test_discrete_share_and_empty_counts_do_not_raise():
    now = 2 * spawns.WINDOW_MS
    for harnesses in ([], ["claude"], ["codex"] + ["claude"] * 6):
        assert spawns.share_drift(_windowed(_picks("a", harnesses, now - 1))) == []


@pytest.mark.parametrize(
    ("codex", "total", "target", "drift"),
    [
        (1, 10, 20, False),
        (3, 10, 20, False),
        (0, 10, 20, True),
        (4, 10, 20, True),
        (1, 11, 20, True),
        (1, 12, 20, True),
        (0, 101, 1, True),
    ],
)
def test_share_drift_allows_at_most_one_pick_from_a_steady_target(codex, total, target, drift):
    now = 2 * spawns.WINDOW_MS
    record = _windowed(_picks("a", ["codex"] * codex + ["claude"] * (total - codex), now - 1)) | {"target": target}
    assert bool(spawns.share_drift(record)) is drift


def test_share_drift_excludes_picks_at_or_before_the_target_change():
    now = 2 * spawns.WINDOW_MS
    old = _picks("old", ["codex"] * 4 + ["claude"] * 13, now - 100)
    edge = _picks("edge", ["codex"] * 4, now - 50)
    recent = _picks("new", ["claude"] * 10, now - 1)
    record = _windowed(recent, [*old, *edge]) | {"target": 0, "target_changed_at": now - 50}
    assert spawns.share_drift(record) == []
    record["agents"] = _picks("new", ["codex"] * 2 + ["claude"] * 8, now - 1)
    [found] = spawns.share_drift(record)
    assert found.measure == 20
    assert "codex 2/10" in found.evidence[0]


def test_share_drift_waits_for_ten_picks_under_a_known_target():
    now = 2 * spawns.WINDOW_MS
    record = _windowed(_picks("old", ["codex"] * 17, now - 100))
    record.update(target=0, target_changed_at=now - 50)
    record["agents"] += _picks("new", ["codex"] * 9, now - 1)
    assert spawns.share_drift(record) == []
    record["agents"] += _picks("tenth", ["codex"], now - 1)
    [found] = spawns.share_drift(record)
    assert found.evidence[0].startswith("codex 10/10")
    record["target_changed_at"] = 0
    assert spawns.share_drift(record) == []
    del record["target_changed_at"]
    assert spawns.share_drift(record) == []


def test_recorded_placements_and_planted_overflow():
    record = load("spawns")
    assert spawns.overflow(record) == []
    planted = copy.deepcopy(record)
    planted["agents"][0]["placement"] = "overflow"
    [found] = spawns.overflow(planted)
    assert found.id == f"account-overflow/{record['agents'][0]['name']}"
    assert found.measure == 1
    assert f"account {record['agents'][0]['account']}" in found.evidence


def test_runtime_records_the_launcher_overflow_placement(tmp_path):
    import subprocess

    import fakeredis

    from scripts.swarm.runtime import HerdrRuntime
    from scripts.swarm.store import RedisStore, SwarmConfig
    from scripts.swarm.tick import tick

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    config = SwarmConfig("sw", str(tmp_path), max_eng=1, max_ci=0, state="running", codex_share=0)
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
