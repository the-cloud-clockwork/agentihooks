import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import affinity, cli
from scripts.swarm.keyspace import ROOT as KEY_ROOT
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
        self.fail_master, self.store, self.orders_at_spawn = fail_master, None, []

    def spawn(self, config, lane, name, task):
        if lane != MASTER:
            return super().spawn(config, lane, name, task)
        order = affinity.pending(self.store, config.slug)
        if order:
            pending = InboxStore(self.store.redis).pending_items(f"master@{config.slug}")
            self.orders_at_spawn.append([i.id for i in pending if i.id == order["item"]])
        if self.fail_master:
            raise SpawnError("codex has no signed in account")
        self.live.add(name)
        self.masters.append((name, dict(task)))
        return Placed(pane_id=f"w1:m{len(self.masters)}", harness=affinity.desired(config) or "claude")


def _start(store, runtime, harness):
    runtime.store = store
    store.update("sw", lanes={MASTER: {"agent": harness}})
    tick("sw", store, tasks(), runtime, 1)
    (boss,) = masters(store)
    assert boss.harness == harness
    return boss


def _orders(store, address="master@sw"):
    pending = InboxStore(store.redis).pending_items(address)
    return [i for i in pending if i.sender == "operator" and i.text.startswith("The operator set the master affinity")]


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
    assert item.id not in [i.id for i in InboxStore(store.redis).pending_items("master@sw")]
    assert runtime.orders_at_spawn == [[]]


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
    assert shown["item"] not in [i.id for i in inbox.pending_items("master@sw")]
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
    assert "the old master is still running, waiting for it to end before starting the next" in actions
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


def _spawn(tmp_path, monkeypatch, lanes, task):
    for key in ("MODEL", "EFFORT"):
        for agent in ("CLAUDE", "CODEX"):
            monkeypatch.delenv(f"AGENTIHOOKS_{agent}_{key}", raising=False)
    seen = {}

    def run_init(argv, **kwargs):
        seen["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    runtime = HerdrRuntime(
        home=tmp_path,
        run=run_init,
        choose=lambda requested, environ: (
            requested or "claude",
            "requested" if isinstance(environ, dict) else "no environment",
        ),
    )
    config = SimpleNamespace(
        slug="sw",
        repo=str(tmp_path),
        code="a1b2c3",
        compact_limit=0,
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
    assert argv[argv.index("--agent") + 1] == new and placed.harness == new and placed.choice == "forced"
    assert argv[argv.index("--profile") + 1] == "master-overlay"
    assert argv[argv.index("--") + 1 :] == FRONTIER[new]


def test_quota_share_routing_never_moves_a_master_off_its_affinity(tmp_path, monkeypatch):
    argv, _ = _spawn(tmp_path, monkeypatch, {MASTER: {"agent": "codex"}}, {"id": MASTER, "peer": ""})
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


def _master(store, harness="claude", state="working", lane=MASTER, name="master@a1b2c3-0001"):
    from scripts.swarm.store import AgentRecord

    store.put_agent("sw", AgentRecord(name, lane, MASTER, harness=harness, state=state, seat="master@sw"))


@pytest.mark.parametrize(("lanes", "expected"), [({}, ""), ({MASTER: {}}, ""), ({MASTER: {"agent": "auto"}}, "")])
def test_no_affinity_reads_as_an_empty_harness(lanes, expected):
    assert affinity.desired(SimpleNamespace(lanes=lanes)) == expected


def test_the_order_is_one_record_under_the_swarm_key_with_every_field(store):
    _master(store)
    store.update("sw", lanes={MASTER: {"agent": "codex"}})
    found = affinity.order(store, "sw", 42)
    (item,) = _orders(store)
    assert found == {
        "to": "codex",
        "from": "claude",
        "master": "master@a1b2c3-0001",
        "item": item.id,
        "at": 42,
        "state": "ordered",
        "reason": "",
    }
    assert store.redis.get(f"{KEY_ROOT}:swarm:sw:master-affinity") is not None
    assert "you run on claude." in item.text


def test_an_order_to_a_master_of_unknown_harness_names_it_unknown(store):
    _master(store, harness="")
    store.update("sw", lanes={MASTER: {"agent": "codex"}})
    affinity.order(store, "sw", 1)
    (item,) = _orders(store)
    assert "you run on an unknown harness." in item.text


def test_without_a_live_master_the_order_returns_what_is_pending_and_sends_nothing(store):
    store.update("sw", lanes={MASTER: {"agent": "codex"}})
    assert affinity.order(store, "sw", 1) is None
    _master(store, state="finished")
    _master(store, lane="eng", name="engineer@a1b2c3-0002")
    assert affinity.order(store, "sw", 2) is None
    assert _orders(store) == []
    store.redis.set(f"{KEY_ROOT}:swarm:sw:master-affinity", '{"to": "codex", "state": "failed"}')
    assert affinity.order(store, "sw", 3) == {"to": "codex", "state": "failed"}


def test_withdrawn_and_handed_off_orders_close_with_their_reason(store):
    _master(store)
    store.update("sw", lanes={MASTER: {"agent": "codex"}})
    first = affinity.order(store, "sw", 1)
    store.update("sw", lanes={MASTER: {"agent": "claude"}})
    affinity.order(store, "sw", 2)
    inbox = InboxStore(store.redis)
    assert inbox.get(first["item"]).reason == "cancelled: the master affinity was set back"
    store.update("sw", lanes={MASTER: {"agent": "codex"}})
    second = affinity.order(store, "sw", 3)
    affinity.handed_off(store, "sw")
    assert inbox.get(second["item"]).reason == "done: the master handed off its seat"


@pytest.mark.parametrize(
    ("harness", "reason"),
    [
        ("claude", "the successor started on claude, not codex"),
        ("", "the successor started on an unknown harness, not codex"),
    ],
)
def test_a_successor_on_the_wrong_harness_fails_the_order_with_its_reason(store, harness, reason):
    _master(store)
    store.update("sw", lanes={MASTER: {"agent": "codex"}})
    affinity.order(store, "sw", 1)
    affinity.placed(store, "sw", harness)
    assert (affinity.pending(store, "sw")["state"], affinity.pending(store, "sw")["reason"]) == ("failed", reason)


def test_the_report_counts_only_a_live_master(store):
    _master(store, state="finished")
    _master(store, harness="codex", lane="eng", name="engineer@a1b2c3-0002")
    config = store.config("sw")
    assert affinity.report(store, "sw", config, store.agents("sw")) == {"desired": "auto", "live": "", "order": None}


@pytest.mark.parametrize(
    ("report", "line"),
    [
        ({"desired": "auto", "live": "", "order": None}, "master affinity  desired auto  live none"),
        (
            {"desired": "codex", "live": "claude", "order": {"to": "codex", "state": "ordered", "reason": ""}},
            "master affinity  desired codex  live claude  order to codex ordered",
        ),
        (
            {"desired": "codex", "live": "", "order": {"to": "codex", "state": "failed", "reason": "no account"}},
            "master affinity  desired codex  live none  order to codex failed: no account",
        ),
    ],
)
def test_the_status_line_names_desired_live_and_order(report, line):
    assert cli._affinity_line(report) == line


def test_swarm_set_and_status_print_the_affinity(env, capsys):
    store, _, rt = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "set", "max-eng-agents=1")
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["master_affinity"] == {
        "desired": "auto",
        "order": None,
    }
    store.update("sw", state="paused", lanes={MASTER: {"agent": "claude"}})
    cli.run_tick(store, "sw", runtime=rt)
    run("sw", "set", "master-agent=codex")
    shown = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["master_affinity"]
    assert shown["desired"] == "codex" and isinstance(shown["order"]["at"], int)
    run("sw", "status")
    assert "master affinity  desired codex  live claude  order to codex ordered" in capsys.readouterr().out


def test_a_claude_only_profile_launches_a_claude_affinity(tmp_path, monkeypatch):
    from scripts.profiles import plugins

    monkeypatch.setattr(plugins, "claude_only", lambda profile: True)
    argv, _ = _spawn(tmp_path, monkeypatch, {MASTER: {"agent": "claude"}}, {"id": MASTER, "peer": ""})
    assert argv[argv.index("--agent") + 1] == "claude"


def test_the_ledger_server_names_the_master_agent_choices():
    from scripts.swarm_ledger import ledger_server

    with pytest.raises(ValueError) as caught:
        ledger_server.control_argv({"action": "set", "master_agent": "auto"})
    assert str(caught.value) == "master_agent must be one of claude, codex"
