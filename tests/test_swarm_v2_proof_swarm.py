import json
from types import SimpleNamespace

import pytest

from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm_v2 import proof_swarm

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]
SLUG = "proof-323133-t351-1"
OTHER = "proof-323133-t351-2"
SETTING = json.dumps({"agent": "engineer@323133-1068", "task": "t351"})
PROJECT = "github.com/the-cloud-clockwork/agentihooks"
ENV = {
    "AGENTIHOOKS_CONTROL_PROOF": SETTING,
    "AGENTIHOOKS_LAUNCH_PROJECTS": f" {PROJECT} ,github.com/the-cloud-clockwork/antoncore",
    "LEDGER_URL": "http://ledger:8765",
}
CLEARED = {key: value for key, value in ENV.items() if key != "AGENTIHOOKS_CONTROL_PROOF"}


@pytest.fixture
def store():
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


class Ledger:
    def __init__(self, named=SLUG, live=(), binned=(), fail=None):
        self.named, self.alive, self.binned, self.fail = named, set(live), set(binned), fail
        self.created, self.removed, self.asked, self.environs = [], [], [], []

    def state(self, slug, environ):
        if self.fail:
            raise self.fail
        self.asked.append(slug)
        self.environs.append(environ)
        return "live" if slug in self.alive else "binned" if slug in self.binned else "missing"

    def create(self, content, environ):
        self.created.append((content, environ))
        return {"slug": self.named, "created": True}

    def remove(self, slug, environ):
        self.removed.append((slug, environ))
        return {"slug": slug, "removed_at": 1}


def swarm(store, slug):
    store.create(SwarmConfig(slug, PROJECT, 1, 0))


def test_a_set_proof_creates_its_ledger_and_a_one_engineer_swarm(store):
    ledger = Ledger()

    assert proof_swarm.reconcile(ENV, store, SLUG, ledger) == [
        f"controller: created proof swarm {SLUG} with one engineer task"
    ]
    ((content, environ),) = ledger.created
    assert (environ["AGENTIHOOKS_AGENT_NAME"], environ["AGENTIHOOKS_SWARM_TASK"]) == ("engineer@323133-1068", "t351")
    assert environ["LEDGER_URL"] == "http://ledger:8765"
    assert content == proof_swarm.CONTENT
    assert [(task["lane"], task["phase"], task["profile"]) for task in content["tasks"]] == [("eng", "p1", "engineer")]
    config = store.config(SLUG)
    assert (config.repo, config.max_eng, config.max_ci, config.state) == (PROJECT, 1, 0, "running")
    assert store.slugs() == [SLUG]


def test_a_restart_after_the_ledger_was_created_only_creates_the_swarm(store):
    ledger = Ledger(live={SLUG})

    assert proof_swarm.reconcile(ENV, store, SLUG, ledger) == [
        f"controller: created proof swarm {SLUG} with one engineer task"
    ]
    assert (ledger.asked, ledger.created, ledger.removed) == ([SLUG], [], [])
    assert ledger.environs == [ENV]
    assert store.slugs() == [SLUG]


def test_a_binned_served_ledger_is_refused_without_creating_another(store):
    ledger = Ledger(binned={SLUG})

    with pytest.raises(SwarmError) as refused:
        proof_swarm.reconcile(ENV, store, SLUG, ledger)

    assert str(refused.value) == f"the proof ledger {SLUG} is in the bin: name a new proof or restore it"
    assert (ledger.created, ledger.removed) == ([], [])
    assert store.slugs() == []


def test_a_served_proof_swarm_that_exists_is_kept_without_a_ledger_call(store):
    swarm(store, SLUG)
    ledger = Ledger()

    assert proof_swarm.reconcile(ENV, store, SLUG, ledger) == []
    assert (ledger.asked, ledger.created, ledger.removed) == ([], [], [])
    assert store.slugs() == [SLUG]


def test_a_proof_named_other_than_the_served_swarm_is_removed_and_refused(store):
    ledger = Ledger(OTHER)

    with pytest.raises(SwarmError) as refused:
        proof_swarm.reconcile(ENV, store, SLUG, ledger)

    assert str(refused.value) == f"the proof ledger is named {OTHER} but the controller serves {SLUG}"
    assert ledger.removed == [(OTHER, ENV)]
    assert store.slugs() == []


def test_a_served_swarm_without_a_proof_name_is_refused_before_any_ledger_call(store):
    ledger = Ledger()

    with pytest.raises(SwarmError) as refused:
        proof_swarm.reconcile(ENV, store, "rig-grade-swarm", ledger)

    assert str(refused.value) == "the controller serves 'rig-grade-swarm', which is not a proof swarm name"
    assert (ledger.asked, ledger.created) == ([], [])


def test_a_cleared_setting_removes_every_proof_swarm_and_only_live_ledgers(store):
    for slug in (SLUG, OTHER, "rig-grade-swarm"):
        swarm(store, slug)
    ledger = Ledger(live={SLUG}, binned={OTHER})

    assert proof_swarm.reconcile(CLEARED, store, SLUG, ledger) == [
        f"controller: removed proof swarm {SLUG}",
        f"controller: removed proof swarm {OTHER}",
    ]
    assert ledger.asked == [SLUG, OTHER]
    assert ledger.environs == [CLEARED, CLEARED]
    assert ledger.removed == [(SLUG, CLEARED)]
    assert store.slugs() == ["rig-grade-swarm"]


def test_a_set_proof_removes_other_proof_swarms_and_keeps_the_served_one(store):
    swarm(store, SLUG)
    swarm(store, OTHER)
    ledger = Ledger(live={OTHER})

    assert proof_swarm.reconcile(ENV, store, SLUG, ledger) == [f"controller: removed proof swarm {OTHER}"]
    assert ledger.removed == [(OTHER, ENV)]
    assert store.slugs() == [SLUG]


def test_a_proof_swarm_with_agents_waits_after_its_ledger_is_removed(store):
    swarm(store, SLUG)
    store.put_agent(SLUG, AgentRecord("engineer@323133-0001", "eng", "t1", started_at=1_000))
    ledger = Ledger(live={SLUG})

    assert proof_swarm.reconcile(CLEARED, store, SLUG, ledger) == [
        f"controller: proof swarm {SLUG} has its ledger removed and waits for its agents to end"
    ]
    assert ledger.removed == [(SLUG, CLEARED)]
    assert store.slugs() == [SLUG]


def test_nothing_happens_without_a_setting_or_a_proof_swarm(store):
    swarm(store, "rig-grade-swarm")
    ledger = Ledger()

    assert proof_swarm.reconcile(CLEARED, store, SLUG, ledger) == []
    assert (ledger.asked, ledger.created, ledger.removed) == ([], [], [])


@pytest.mark.parametrize(
    "setting", ["{", "[]", json.dumps({"agent": "engineer@323133-1068"}), json.dumps({"task": "t351"})]
)
def test_a_malformed_setting_is_refused(store, setting):
    with pytest.raises(SwarmError) as refused:
        proof_swarm.reconcile({**ENV, "AGENTIHOOKS_CONTROL_PROOF": setting}, store, SLUG, Ledger())

    assert str(refused.value) == "AGENTIHOOKS_CONTROL_PROOF must be a JSON object naming an agent and a task"


@pytest.mark.parametrize("projects", [None, " , "])
def test_a_proof_without_launch_projects_is_refused(store, projects):
    environ = {key: value for key, value in ENV.items() if key != "AGENTIHOOKS_LAUNCH_PROJECTS"}
    if projects is not None:
        environ["AGENTIHOOKS_LAUNCH_PROJECTS"] = projects

    with pytest.raises(SwarmError) as refused:
        proof_swarm.reconcile(environ, store, SLUG, Ledger())

    assert str(refused.value) == "a proof swarm needs AGENTIHOOKS_LAUNCH_PROJECTS"
    assert store.slugs() == []


@pytest.mark.parametrize("fail", [SystemExit("server refused: 403 nope"), ConnectionRefusedError("refused")])
def test_a_failing_ledger_route_is_a_swarm_error(store, fail):
    with pytest.raises(SwarmError) as refused:
        proof_swarm.reconcile(ENV, store, SLUG, Ledger(fail=fail))

    assert str(refused.value) == f"the proof ledger route failed: {fail}"


@pytest.mark.parametrize(("deployment", "called"), [("distributed", True), ("local", False)])
def test_a_pass_runs_only_against_a_hosted_ledger_and_prints_each_line(store, monkeypatch, capsys, deployment, called):
    from scripts.swarm_ledger import proof_ledger

    seen = []
    monkeypatch.setattr(proof_swarm, "reconcile", lambda *args: seen.append(args) or ["controller: a", "controller: b"])
    environ = {**ENV, "AGENTIHOOKS_DEPLOYMENT": deployment, "AGENTIHOOKS_CONTROL_SWARM": SLUG}

    proof_swarm.run(environ, store)

    assert seen == ([(environ, store, SLUG, proof_ledger)] if called else [])
    assert capsys.readouterr().out == ("controller: a\ncontroller: b\n" if called else "")


def test_a_pass_without_a_served_swarm_reconciles_the_empty_name(store, monkeypatch):
    seen = []
    monkeypatch.setattr(proof_swarm, "reconcile", lambda *args: seen.append(args[2]) or [])

    proof_swarm.run({"AGENTIHOOKS_DEPLOYMENT": "distributed"}, store)

    assert seen == [""]


def test_the_controller_runs_a_pass_at_start_and_after_each_tick(store, monkeypatch):
    from scripts import operator_env
    from scripts.swarm import commands, controller
    from scripts.swarm_v2 import control_service

    order = []
    monkeypatch.setattr(operator_env, "fill", lambda environ: None)
    monkeypatch.setattr(commands, "hive_id", lambda: "hive-fixture")
    monkeypatch.setattr(controller, "connect", lambda: store)
    monkeypatch.setattr(proof_swarm, "run", lambda environ, given: order.append(("proof", given)))
    monkeypatch.setattr(control_service, "host", lambda *args: order.append(("host", args[1])))
    monkeypatch.setattr(controller, "tick_once", lambda given, service: order.append(("tick", given)) or {})

    assert controller.main(["run", "--once"]) == 0
    assert order == [("proof", store), ("host", store), ("tick", store), ("proof", store)]


def test_a_failed_pass_is_reported_and_the_controller_keeps_running(store, capsys):
    environ = {**ENV, "AGENTIHOOKS_DEPLOYMENT": "distributed", "AGENTIHOOKS_CONTROL_SWARM": SLUG}

    proof_swarm.run({**environ, "AGENTIHOOKS_CONTROL_PROOF": "{"}, store)

    assert capsys.readouterr() == (
        "",
        "controller: the proof swarm pass failed and runs again after the next tick: "
        "AGENTIHOOKS_CONTROL_PROOF must be a JSON object naming an agent and a task\n",
    )


def test_every_pass_line_is_flushed_for_the_pod_log(store, monkeypatch):
    printed = []
    monkeypatch.setattr(proof_swarm, "print", lambda *args, **kwargs: printed.append((args, kwargs)), raising=False)
    environ = {**ENV, "AGENTIHOOKS_DEPLOYMENT": "distributed", "AGENTIHOOKS_CONTROL_SWARM": SLUG}
    monkeypatch.setattr(proof_swarm, "reconcile", lambda *args: ["controller: a"])

    proof_swarm.run(environ, store)

    def refuse(*args):
        raise SwarmError("down")

    monkeypatch.setattr(proof_swarm, "reconcile", refuse)
    proof_swarm.run(environ, store)

    assert [kwargs["flush"] for _, kwargs in printed] == [True, True]
    assert printed[0][0] == ("controller: a",)


class Recovery:
    def __init__(self, slug=SLUG, waiting=("exe-a", "exe-b")):
        self.slug, self.waiting, self.decided = slug, list(waiting), []
        self.checkpoints = SimpleNamespace(list=lambda execution: [])

    def pending(self):
        return list(self.waiting)

    def decide(self, execution_id, choice):
        self.decided.append((execution_id, choice))


SERVED = {**ENV, "AGENTIHOOKS_CONTROL_SWARM": SLUG}


def test_the_served_proof_swarm_chooses_fresh_for_every_pending_recovery_with_its_reason():
    recovery = Recovery()

    lines = proof_swarm.choose_fresh(SERVED, recovery)

    assert recovery.decided == [("exe-a", "fresh"), ("exe-b", "fresh")]
    assert lines == [
        f"controller: chose fresh for {execution} in proof swarm {SLUG}: the controller owns this proof swarm, "
        "which keeps no checkpoints and runs one trivial task"
        for execution in ("exe-a", "exe-b")
    ]


@pytest.mark.parametrize("continuation", ["native", "context"])
def test_proof_recovery_preserves_checkpoint_offers_for_an_explicit_restore(continuation):
    recovery = Recovery()
    recovery.checkpoints = SimpleNamespace(
        list=lambda execution: (
            [{"checkpoint_id": "ckp-one", "status": "complete", "continuation": continuation}]
            if execution == "exe-a"
            else []
        )
    )
    lines = proof_swarm.choose_fresh(SERVED, recovery)
    assert recovery.decided == [("exe-b", "fresh")]
    assert len(lines) == 1
    assert "exe-b" in lines[0]


@pytest.mark.parametrize(
    ("environ", "slug"),
    [
        (SERVED, OTHER),
        ({**CLEARED, "AGENTIHOOKS_CONTROL_SWARM": SLUG}, SLUG),
        ({**SERVED, "AGENTIHOOKS_CONTROL_PROOF": "{"}, SLUG),
        ({**ENV, "AGENTIHOOKS_CONTROL_SWARM": "rig-grade-swarm"}, "rig-grade-swarm"),
        (ENV, SLUG),
    ],
    ids=["another-swarm", "proof-cleared", "proof-malformed", "not-a-proof-name", "nothing-served"],
)
def test_no_swarm_but_the_served_proof_has_a_pending_recovery_decided(environ, slug):
    recovery = Recovery(slug)

    assert proof_swarm.choose_fresh(environ, recovery) == []
    assert recovery.decided == []
