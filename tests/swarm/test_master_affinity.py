from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import affinity, cli
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.status import status_report
from scripts.swarm.store import MASTER, SwarmConfig
from scripts.swarm.tick import Placed, SpawnError, tick
from tests.swarm import test_cli, test_tick
from tests.swarm.profile_fixture import validated
from tests.swarm.test_cli import run
from tests.swarm.test_tick import FakeRuntime, masters, tasks

pytestmark = pytest.mark.xdist_group("fakeredis")
store, env = test_tick.store, test_cli.env

FRONTIER = {
    "claude": ["--model", "opus", "--effort", "high"],
    "codex": ["-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="high"'],
}
OTHER = {"claude": "codex", "codex": "claude"}


class AffinityRuntime(FakeRuntime):
    def __init__(self, fail_master=False):
        super().__init__()
        self.fail_master = fail_master

    def spawn(self, config, lane, name, task, spawns=None):
        if lane != MASTER:
            return super().spawn(config, lane, name, task, spawns)
        if self.fail_master:
            raise SpawnError("codex has no signed in account")
        self.live.add(name)
        self.masters.append((name, dict(task)))
        return Placed(pane_id=f"w1:m{len(self.masters)}", harness=affinity.desired(config) or "claude")


def _start(store, runtime, harness):
    store.update("sw", lanes={MASTER: {"agent": harness}})
    tick("sw", store, tasks(), runtime, 1)
    (boss,) = masters(store)
    assert boss.harness == harness
    return boss


def _orders(store, address="master@sw"):
    return [i for i in InboxStore(store.redis).pending_items(address) if i.sender == "operator"]


@pytest.mark.parametrize("start", ["claude", "codex"])
def test_changing_the_affinity_orders_one_handoff_and_the_next_master_runs_on_the_new_harness(store, start):
    runtime = AffinityRuntime()
    boss = _start(store, runtime, start)
    store.update("sw", lanes={MASTER: {"agent": OTHER[start]}})
    first = affinity.order(store, "sw", 5)
    again = affinity.order(store, "sw", 6)
    assert first["to"] == OTHER[start] and first["from"] == start and first["master"] == boss.name
    assert again == first
    (item,) = _orders(store)
    assert "agentihooks swarm sw handoff" in item.text and OTHER[start] in item.text
    store.put_handoff("sw", MASTER, "# Handoff\n<!-- handoff complete -->")
    store.put_agent("sw", replace(boss, state="finished"))
    actions = tick("sw", store, tasks(), runtime, 7)
    assert actions.index(f"retired {boss.name}") < actions.index("spawned master master@a1b2c3-0002")
    (successor,) = masters(store)
    assert successor.harness == OTHER[start] and successor.seat == boss.seat
    assert runtime.live & {boss.name, successor.name} == {successor.name}
    assert affinity.pending(store, "sw") is None


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_an_unchanged_affinity_orders_nothing_and_restarts_nothing(store, harness):
    runtime = AffinityRuntime()
    boss = _start(store, runtime, harness)
    store.update("sw", lanes={MASTER: {"agent": harness}})
    assert affinity.order(store, "sw", 5) is None
    assert _orders(store) == []
    tick("sw", store, tasks(), runtime, 6)
    assert [a.name for a in masters(store)] == [boss.name] and runtime.killed == []


def test_setting_back_to_the_live_harness_withdraws_a_pending_order(store):
    runtime = AffinityRuntime()
    _start(store, runtime, "claude")
    store.update("sw", lanes={MASTER: {"agent": "codex"}})
    affinity.order(store, "sw", 5)
    store.update("sw", lanes={MASTER: {"agent": "claude"}})
    assert affinity.order(store, "sw", 6) is None
    assert affinity.pending(store, "sw") is None and _orders(store) == []


def test_a_failed_successor_launch_is_shown_and_keeps_the_handoff_and_the_seat_inbox(store):
    runtime = AffinityRuntime()
    boss = _start(store, runtime, "claude")
    store.update("sw", lanes={MASTER: {"agent": "codex"}})
    affinity.order(store, "sw", 5)
    inbox = InboxStore(store.redis)
    waiting = inbox.send("operator", "master@sw", "raise the eng cap to four")
    store.put_handoff("sw", MASTER, "# Handoff\nkeep the caps\n<!-- handoff complete -->")
    store.put_agent("sw", replace(boss, state="finished"))
    runtime.fail_master = True
    actions = tick("sw", store, tasks(), runtime, 7)
    assert "master spawn failed: codex has no signed in account" in actions
    shown = affinity.pending(store, "sw")
    assert shown["state"] == "failed" and shown["reason"] == "codex has no signed in account"
    assert store.handoff("sw", MASTER).startswith("# Handoff")
    assert waiting.id in [i.id for i in inbox.pending_items("master@sw")]
    runtime.fail_master = False
    tick("sw", store, tasks(), runtime, 8)
    (successor,) = masters(store)
    assert successor.harness == "codex" and affinity.pending(store, "sw") is None
    assert runtime.masters[-1][1]["handoff"].startswith("# Handoff")
    assert waiting.id in [i.id for i in inbox.pending_items("master@sw")]


def test_a_finished_master_still_alive_blocks_its_replacement(store):
    runtime = AffinityRuntime()
    boss = _start(store, runtime, "claude")
    store.put_agent("sw", replace(boss, state="finished"))
    runtime.stuck.add(boss.name)
    actions = tick("sw", store, tasks(), runtime, 7)
    assert "spawned master master@a1b2c3-0002" not in actions
    assert len(runtime.masters) == 1 and boss.name in runtime.live
    runtime.stuck.discard(boss.name)
    tick("sw", store, tasks(), runtime, 8)
    assert [a.name for a in masters(store)] == ["master@a1b2c3-0002"] and runtime.live == {"master@a1b2c3-0002"}


def test_status_reports_desired_and_live_harness_and_the_order(store):
    runtime = AffinityRuntime()
    _start(store, runtime, "claude")
    store.update("sw", lanes={MASTER: {"agent": "codex"}})
    affinity.order(store, "sw", 5)
    report = status_report(store, "sw", {"tasks": [], "_meta": {"events": []}})["master_affinity"]
    assert (report["desired"], report["live"], report["order"]["state"]) == ("codex", "claude", "ordered")


def test_snapshot_and_restore_keep_the_affinity_and_a_pending_order(store):
    runtime = AffinityRuntime()
    _start(store, runtime, "claude")
    store.update("sw", lanes={MASTER: {"agent": "codex"}})
    order = affinity.order(store, "sw", 5)
    saved = store.export("sw")
    store.update("sw", lanes={MASTER: {"agent": "claude"}})
    store.redis.delete(store.key("sw", "master-affinity"))
    store.restore("sw", saved)
    assert affinity.desired(store.config("sw")) == "codex"
    assert affinity.pending(store, "sw") == order


def test_a_template_saved_from_the_swarm_carries_the_master_affinity(store):
    from scripts.swarm import templates

    store.update("sw", lanes={MASTER: {"agent": "codex"}})
    template = templates.from_config("affine", store.config("sw"))
    assert template.lanes[MASTER].agent == "codex"
    assert templates.lane_map(template)[MASTER]["agent"] == "codex"


def test_swarm_set_master_agent_orders_the_live_master_once(env, capsys):
    store, _, rt = env
    run("sw", "create", "--repo", "/repo")
    store.update("sw", state="paused", lanes={MASTER: {"agent": "claude"}})
    cli.run_tick(store, "sw", runtime=rt)
    assert [a.harness for a in masters(store)] == ["claude"]
    capsys.readouterr()
    assert run("sw", "set", "master-agent=codex") == 0
    out = capsys.readouterr().out.strip().splitlines()[-1]
    assert '"master_affinity"' in out and '"ordered"' in out
    assert run("sw", "set", "master-agent=codex") == 0
    assert len(_orders(store)) == 1
    assert store.config("sw").lanes[MASTER]["agent"] == "codex"


def _spawn(tmp_path, monkeypatch, lanes, task, codex_share=None):
    for key in ("MODEL", "EFFORT"):
        for agent in ("CLAUDE", "CODEX"):
            monkeypatch.delenv(f"AGENTIHOOKS_{agent}_{key}", raising=False)
    seen = {}

    def run_init(argv, **kwargs):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(
        home=tmp_path, run=run_init, choose=lambda requested, environ: (requested or "claude", "requested")
    )
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
        codex_share=codex_share,
        codex_min_week_left=0,
        lanes=lanes,
        autonomy="delegate",
    )
    placed = runtime.spawn(config, MASTER, "master@a1b2c3-0002", task)
    argv = seen["argv"]
    return argv, placed


@pytest.mark.parametrize("old", ["claude", "codex"])
def test_a_handoff_successor_launches_on_the_desired_harness_with_the_original_profile(tmp_path, monkeypatch, old):
    new = OTHER[old]
    task = {
        "id": MASTER,
        "peer": "",
        "handoff": "# Handoff\n<!-- handoff complete -->",
        "handoff_envelope": {
            "launch": {
                "profile": "master-overlay",
                "harness": old,
                "model": "opus" if old == "claude" else "gpt-6.1-sol",
                "effort": "high",
                "account": "acct-old",
            }
        },
    }
    argv, placed = _spawn(tmp_path, monkeypatch, {MASTER: {"agent": new}}, task)
    assert argv[argv.index("--agent") + 1] == new and placed.harness == new
    assert argv[argv.index("--profile") + 1] == "master-overlay"
    assert argv[argv.index("--") + 1 :] == FRONTIER[new]


def test_quota_share_routing_never_moves_a_master_off_its_affinity(tmp_path, monkeypatch):
    argv, _ = _spawn(tmp_path, monkeypatch, {MASTER: {"agent": "codex"}}, {"id": MASTER, "peer": ""}, codex_share=0)
    assert argv[argv.index("--agent") + 1] == "codex"


def test_a_claude_only_profile_refuses_a_codex_affinity_visibly(tmp_path, monkeypatch):
    from scripts.profiles import plugins

    monkeypatch.setattr(plugins, "claude_only", lambda profile: True)
    with pytest.raises(SpawnError, match="claude only"):
        _spawn(tmp_path, monkeypatch, {MASTER: {"agent": "codex", "profile": "master"}}, {"id": MASTER, "peer": ""})


def test_the_ledger_server_maps_master_agent_to_the_swarm_set_pair():
    from scripts.swarm_ledger import ledger_server

    assert ledger_server.control_argv({"action": "set", "master_agent": "codex"}) == ["set", "master-agent=codex"]
    with pytest.raises(ValueError, match="master_agent"):
        ledger_server.control_argv({"action": "set", "master_agent": "auto"})


def test_a_swarm_created_from_scratch_keeps_its_affinity_through_the_config_round_trip(store):
    store.create(SwarmConfig("sx", "/repo", max_eng=0, max_ci=0, lanes={MASTER: {"agent": "codex"}}))
    assert affinity.desired(store.config("sx")) == "codex"
