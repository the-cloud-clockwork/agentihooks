import json
from dataclasses import asdict
from pathlib import Path

import pytest

from scripts.swarm.keyspace import ROOT
from scripts.swarm.store import SwarmError
from scripts.swarm_v2 import accounts
from scripts.swarm_v2.accounts import OCCUPIED, RESERVED, AccountCapacity
from scripts.swarm_v2.reconciliation import accounts as reconciliation
from scripts.swarm_v2.reconciliation.accounts import AccountReconciler, Finding
from scripts.swarm_v2.registry import STALE_AFTER_MS, FleetRegistry, Scope, Session
from scripts.swarm_v2.runtime import observe
from tests.sv2_ctl02_cases import build

pytestmark = pytest.mark.unit

INPUTS = json.loads((Path(__file__).parent / "fixtures/swarm_v2/account-reconciliation.json").read_text())
SLUG = "fixture"
ACCOUNT = INPUTS["account"]
CAP = INPUTS["cap"]
TTL = INPUTS["ttl_ms"]
HANDOFF, ORPHAN, TERMINAL = INPUTS["handoff_seat"], INPUTS["orphan_seat"], INPUTS["terminal_seat"]
MACHINE = Scope(**INPUTS["machine"])
LOCAL_MACHINE = Scope("local", "workstation", "pid:[4026531836]")
RETIRING = f"{SLUG}/{HANDOFF}#1"
SUCCESSOR = f"{SLUG}/{HANDOFF}"
ORPHANED = f"{SLUG}/{ORPHAN}"
LOST_TERMINAL = f"{SLUG}/{TERMINAL}"


class World:
    def __init__(self, monkeypatch):
        self.store, self.authority, _, self.clock, self.start = build(monkeypatch)
        self.fleet = FleetRegistry(self.store, SLUG, self.authority.authorize, lambda: self.clock[0])
        self.capacity = AccountCapacity(self.store, SLUG, self.authority.authorize, lambda: self.clock[0])
        self.reconciler = self.restart({})
        self.agents, self.tokens, self.predecessor = {}, {}, None

    def restart(self, environ):
        return AccountReconciler(self.store, SLUG, lambda: self.clock[0], environ)

    def launch(self, seat):
        current = self.agents.get(seat)
        agent, token = self.start(seat=seat, previous=current.execution_id if current else "")
        self.agents[seat], self.tokens[seat] = agent, token
        return agent, token

    @staticmethod
    def session(agent, scope=MACHINE):
        return Session(
            f"sess-{agent.name}", scope, 4242, 7, "claude", agent.name, "", agent.execution_id, agent.generation
        )

    def confirm(self, agent, token):
        self.fleet.register(self.session(agent), token)
        return self.capacity.occupy(token, MACHINE, self.session(agent).session_id)

    def running(self, seat):
        agent, token = self.launch(seat)
        self.capacity.reserve(token, CAP, TTL)
        return self.confirm(agent, token)

    def observed(self, agent, case, generation=None, sources=None):
        values = INPUTS[case]
        seen = observe.Classification(
            agent.execution_id,
            agent.generation if generation is None else generation,
            observe.State(values["state"]),
            observe.Terminal(values["terminal"]),
            observe.Failure(values["failure"]),
            observe.Confidence(values["confidence"]),
            1.0,
            1.0,
            1.0,
            INPUTS[sources] if sources else {},
        )
        self.store.redis.hset(self.store.key(SLUG, "observations"), agent.execution_id, json.dumps(asdict(seen)))

    def scene(self):
        self.running(TERMINAL)
        self.observed(self.agents[TERMINAL], "lost_terminal")
        self.running(HANDOFF)
        _, orphan = self.launch(ORPHAN)
        self.capacity.reserve(orphan, CAP, TTL)
        self.launch(ORPHAN)
        self.predecessor = self.agents[HANDOFF]
        _, successor = self.launch(HANDOFF)
        self.capacity.reserve(successor, CAP, TTL)

    def rows(self):
        return self.store.redis.hgetall(f"{ROOT}:accounts:{ACCOUNT}")

    def row(self, name):
        return json.loads(self.rows()[name])


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


def refusal(call, *args):
    with pytest.raises(SwarmError) as error:
        call(*args)
    return str(error.value)


def kinds(found):
    keyed = {finding.holder: (finding.kind, finding.action) for finding in found}
    assert len(keyed) == len(found)
    return keyed


@pytest.mark.parametrize("run", ["first", "second"])
def test_a_handoff_releases_the_old_slot_only_after_the_successor_occupies(world, run):
    world.scene()
    successor = world.agents[HANDOFF]

    assert sorted(world.rows()) == sorted([LOST_TERMINAL, ORPHANED, RETIRING, SUCCESSOR]), run
    assert accounts.stored_accounts(world.store) == [ACCOUNT]
    assert (world.row(RETIRING)["execution_id"], world.row(RETIRING)["state"]) == (
        world.predecessor.execution_id,
        OCCUPIED,
    )
    held = world.reconciler.reconcile()

    assert kinds(held) == {
        RETIRING: ("handoff_unconfirmed", "keep"),
        ORPHANED: ("orphan_reservation", "keep"),
        LOST_TERMINAL: ("terminal_loss", "keep"),
    }
    assert {f.holder: f.evidence for f in held if f.holder in (RETIRING, ORPHANED)} == {
        RETIRING: "successor occupancy unconfirmed",
        ORPHANED: "not the seat's current execution",
    }
    assert sorted(world.rows()) == sorted([LOST_TERMINAL, ORPHANED, RETIRING, SUCCESSOR])
    assert world.reconciler.account_occupancy_discrepancies() == {"handoff_unconfirmed": 1, "orphan_reservation": 1}
    assert world.reconciler.account_occupancy_discrepancies_total() == 2

    world.confirm(successor, world.tokens[HANDOFF])
    done = world.reconciler.reconcile()

    assert kinds(done) == {
        RETIRING: ("handoff_confirmed", "release"),
        ORPHANED: ("orphan_reservation", "keep"),
        LOST_TERMINAL: ("terminal_loss", "keep"),
    }
    assert sorted(world.rows()) == sorted([LOST_TERMINAL, ORPHANED, SUCCESSOR])
    stored = world.row(SUCCESSOR)
    assert (stored["execution_id"], stored["generation"], stored["state"]) == (successor.execution_id, 2, OCCUPIED)
    assert world.reconciler.account_occupancy_discrepancies() == {"orphan_reservation": 1}
    assert world.reconciler.account_occupancy_discrepancies_total() == 1
    assert world.store.redis.hgetall(world.store.key(SLUG, "account-occupancy-discrepancies")) == {
        "orphan_reservation": "1"
    }


def test_a_successor_that_never_confirms_leaves_the_old_slot_counted(world):
    world.scene()
    world.clock[0] += TTL

    found = world.reconciler.reconcile()

    assert kinds(found) == {
        RETIRING: ("handoff_unconfirmed", "keep"),
        SUCCESSOR: ("expired_reservation", "release"),
        ORPHANED: ("expired_reservation", "release"),
        LOST_TERMINAL: ("terminal_loss", "keep"),
    }
    successor = world.agents[HANDOFF]
    assert [f for f in found if f.holder == SUCCESSOR] == [
        Finding("expired_reservation", "release", ACCOUNT, SUCCESSOR, successor.execution_id, 2, "")
    ]
    assert sorted(world.rows()) == sorted([LOST_TERMINAL, RETIRING])
    assert [slot.holder for slot in world.capacity.slots(ACCOUNT)] == [RETIRING, LOST_TERMINAL]
    assert world.reconciler.account_occupancy_discrepancies() == {"handoff_unconfirmed": 1}


def test_b_a_terminal_failure_alone_frees_no_slot_and_admits_no_duplicate(world):
    world.scene()
    lost = world.agents[TERMINAL]
    before = world.rows()

    for source in ("terminal", "heartbeat", "provider", "ssh", "", "kubernetes", "supervisor"):
        assert refusal(world.reconciler.exited, lost.execution_id, lost.generation, source) == "insufficient_evidence"
    kept = world.reconciler.reconcile()
    _, duplicate = world.launch(TERMINAL)

    assert refusal(world.capacity.reserve, duplicate, CAP, TTL) == "account_full"
    assert kinds(kept)[LOST_TERMINAL] == ("terminal_loss", "keep")
    assert world.rows() == before
    assert world.reconciler.stale_exit_events() == 0

    replaced = world.reconciler.reconcile()

    assert kinds(replaced)[LOST_TERMINAL] == ("orphan_occupancy", "keep")
    assert [f.evidence for f in replaced if f.holder == LOST_TERMINAL] == ["working/terminal_loss"]
    assert world.rows() == before
    assert world.reconciler.account_occupancy_discrepancies() == {
        "handoff_unconfirmed": 1,
        "orphan_occupancy": 1,
        "orphan_reservation": 1,
    }


def test_another_seat_cannot_take_a_slot_a_handoff_still_holds(world):
    world.scene()
    _, other = world.launch("eng-4@fixture")
    before = world.rows()

    assert refusal(world.capacity.reserve, other, CAP, TTL) == "account_full"
    assert world.rows() == before


@pytest.mark.parametrize(
    ("case", "generation", "found"),
    [
        ("suspect_worker", None, {LOST_TERMINAL: ("orphan_occupancy", "keep", "suspect/worker_loss")}),
        ("lost_terminal", None, {LOST_TERMINAL: ("terminal_loss", "keep", "working/terminal_loss")}),
        ("lost_worker", None, {LOST_TERMINAL: ("orphan_occupancy", "release", "lost/worker_loss")}),
        ("lost_worker", 7, {}),
        (None, None, {}),
    ],
)
def test_an_occupancy_is_released_only_on_a_lost_classification_of_its_generation(world, case, generation, found):
    world.running(TERMINAL)
    if case:
        world.observed(world.agents[TERMINAL], case, generation)

    result = world.reconciler.reconcile()

    assert {f.holder: (f.kind, f.action, f.evidence) for f in result} == found
    assert (LOST_TERMINAL in world.rows()) is (found.get(LOST_TERMINAL, ("", "keep"))[1] == "keep")


def test_a_lost_current_worker_is_released_and_its_seat_can_be_refilled(world):
    world.running(TERMINAL)
    world.observed(world.agents[TERMINAL], "lost_worker")

    [finding] = world.reconciler.reconcile()
    _, replacement = world.launch(TERMINAL)
    slot = world.capacity.reserve(replacement, CAP, TTL)

    assert (finding.kind, finding.action, finding.evidence) == ("orphan_occupancy", "release", "lost/worker_loss")
    assert (slot.generation, slot.state) == (2, RESERVED)
    assert sorted(world.rows()) == [LOST_TERMINAL]


def test_an_unobserved_replaced_occupancy_is_kept_and_named_unobserved(world):
    world.running(TERMINAL)
    world.launch(TERMINAL)

    [finding] = world.reconciler.reconcile()

    assert (finding.kind, finding.action, finding.evidence) == ("orphan_occupancy", "keep", "unobserved")
    assert sorted(world.rows()) == [LOST_TERMINAL]


def test_a_retiring_row_of_the_current_generation_is_never_a_confirmed_handoff(world):
    world.running(TERMINAL)
    world.store.redis.hset(
        f"{ROOT}:accounts:{ACCOUNT}",
        f"{LOST_TERMINAL}#1",
        json.dumps({**world.row(LOST_TERMINAL), "holder": f"{LOST_TERMINAL}#1"}),
    )

    found = world.reconciler.reconcile()

    assert kinds(found) == {f"{LOST_TERMINAL}#1": ("handoff_unconfirmed", "keep")}
    assert f"{LOST_TERMINAL}#1" in world.rows()


def test_the_reconciler_judges_expiry_by_its_own_clock_and_holds_no_grant(world):
    world.scene()
    later = AccountReconciler(world.store, SLUG, lambda: world.clock[0] + TTL, {})

    assert kinds(later.report())[ORPHANED] == ("expired_reservation", "release")
    assert kinds(world.reconciler.report())[ORPHANED] == ("orphan_reservation", "keep")
    assert refusal(later.capacity.reserve, world.tokens[HANDOFF], CAP, TTL) == "forbidden_scope"


def test_a_worker_waiting_on_quota_keeps_its_slot_until_its_session_closes(world):
    world.running(TERMINAL)
    agent = world.agents[TERMINAL]
    world.observed(agent, "waiting_worker")
    world.clock[0] += 2 * STALE_AFTER_MS
    assert world.fleet.sweep() == 1

    waiting = world.reconciler.reconcile()

    assert kinds(waiting) == {LOST_TERMINAL: ("waiting_quota", "keep")}
    assert world.row(LOST_TERMINAL)["state"] == OCCUPIED
    assert world.reconciler.account_occupancy_discrepancies() == {}

    world.fleet.close(MACHINE, world.session(agent).session_id, world.tokens[TERMINAL])
    ended = world.reconciler.reconcile()

    assert kinds(ended) == {LOST_TERMINAL: ("ended_session", "release")}
    assert world.rows() == {}


def test_a_live_remote_session_without_a_slot_is_reported_and_a_local_one_is_not(world):
    remote, remote_token = world.launch(TERMINAL)
    world.fleet.register(world.session(remote), remote_token)
    local, local_token = world.launch(ORPHAN)
    world.fleet.register(world.session(local, LOCAL_MACHINE), local_token)

    found = world.reconciler.report()

    assert found == [
        Finding("unaccounted_session", "keep", "", LOST_TERMINAL, remote.execution_id, 1, "live session holds no slot")
    ]
    assert world.rows() == {}


def test_report_writes_nothing(world):
    world.scene()
    world.clock[0] += TTL
    before = world.rows()

    found = world.reconciler.report()

    assert kinds(found)[ORPHANED] == ("expired_reservation", "release")
    assert world.rows() == before
    assert world.store.redis.exists(world.store.key(SLUG, "account-occupancy-discrepancies")) == 0


def test_c_a_restarted_controller_reconciles_expiry_and_occupancy_without_reading_processes(world, monkeypatch):
    world.scene()
    predecessor = world.predecessor
    world.clock[0] += TTL

    def forbidden(*_args, **_kwargs):
        raise AssertionError("reconciliation read a process table or process environment")

    monkeypatch.setattr("hooks.proc.processes", forbidden)
    monkeypatch.setattr("hooks.context.account_sessions._env_names", forbidden)
    monkeypatch.setattr("hooks.context.account_sessions.session_account", forbidden)
    restarted = world.restart({})

    first = restarted.reconcile()
    after = world.rows()
    second = restarted.reconcile()

    assert kinds(first)[ORPHANED] == kinds(first)[SUCCESSOR] == ("expired_reservation", "release")
    assert sorted(after) == sorted([LOST_TERMINAL, RETIRING])
    assert kinds(second) == {RETIRING: ("handoff_unconfirmed", "keep"), LOST_TERMINAL: ("terminal_loss", "keep")}
    assert world.rows() == after

    world.observed(predecessor, "lost_worker", sources="supervisor_exit")
    exited = restarted.exited(predecessor.execution_id, predecessor.generation, "supervisor")
    replay = restarted.exited(predecessor.execution_id, predecessor.generation, "supervisor")

    assert (exited.kind, exited.action, exited.holder) == ("exited", "release", RETIRING)
    assert (replay.kind, replay.action, replay.holder) == ("stale_exit", "keep", "")
    assert sorted(world.rows()) == [LOST_TERMINAL]
    assert restarted.stale_exit_events() == 1
    assert restarted.account_occupancy_discrepancies() == {"handoff_unconfirmed": 1}

    settled = restarted.reconcile()

    assert kinds(settled) == {LOST_TERMINAL: ("terminal_loss", "keep")}
    assert restarted.account_occupancy_discrepancies() == {}


def test_a_delayed_exit_of_a_replaced_generation_never_frees_the_newer_slot(world):
    world.scene()
    successor = world.agents[HANDOFF]
    world.confirm(successor, world.tokens[HANDOFF])
    world.reconciler.reconcile()
    newer = world.rows()[SUCCESSOR]

    late = world.reconciler.exited(world.predecessor.execution_id, 1, "kubernetes")
    wrong = world.reconciler.exited(successor.execution_id, 1, "kubernetes")

    assert late == Finding("stale_exit", "keep", "", "", world.predecessor.execution_id, 1, "kubernetes")
    assert (wrong.kind, wrong.action) == ("stale_exit", "keep")
    assert world.rows()[SUCCESSOR] == newer
    assert world.reconciler.stale_exit_events() == 2
    assert world.store.redis.get(world.store.key(SLUG, "account-stale-exits")) == "2"


def test_an_exit_of_the_current_generation_frees_only_its_own_row(world):
    world.scene()
    lost = world.agents[TERMINAL]
    world.observed(lost, "lost_worker", sources="pod_failed")
    before = world.rows()

    found = world.reconciler.exited(lost.execution_id, lost.generation, "kubernetes")

    assert (found.kind, found.action, found.account, found.holder) == ("exited", "release", ACCOUNT, LOST_TERMINAL)
    assert (found.execution_id, found.generation, found.evidence) == (lost.execution_id, 1, "kubernetes")
    assert world.rows() == {name: raw for name, raw in before.items() if name != LOST_TERMINAL}


@pytest.mark.parametrize(
    ("sources", "source", "generation", "released"),
    [
        ("pod_gone", "kubernetes", None, False),
        ("pod_failed", "kubernetes", None, True),
        ("supervisor_exit", "supervisor", None, True),
        ("pod_unreachable", "kubernetes", None, False),
        ("pod_running", "kubernetes", None, False),
        ("supervisor_exit", "kubernetes", None, False),
        ("pod_failed", "supervisor", None, False),
        ("pod_failed", "kubernetes", 7, False),
    ],
)
def test_an_exit_releases_only_on_its_stored_observation(world, sources, source, generation, released):
    world.running(TERMINAL)
    lost = world.agents[TERMINAL]
    world.observed(lost, "lost_terminal", generation, sources)
    before = world.rows()

    if released:
        assert world.reconciler.exited(lost.execution_id, lost.generation, source).action == "release"
        assert world.rows() == {}
    else:
        assert refusal(world.reconciler.exited, lost.execution_id, lost.generation, source) == "insufficient_evidence"
        assert world.rows() == before
    assert world.reconciler.stale_exit_events() == 0


def test_observe_only_mode_reports_releases_and_frees_nothing(world):
    world.scene()
    world.confirm(world.agents[HANDOFF], world.tokens[HANDOFF])
    world.clock[0] += TTL
    observer = world.restart({reconciliation.MODE: "observe"})
    before = world.rows()

    found = observer.reconcile()
    lost = world.agents[TERMINAL]
    world.observed(lost, "lost_worker", sources="supervisor_exit")
    exited = observer.exited(lost.execution_id, lost.generation, "supervisor")

    assert kinds(found) == {
        RETIRING: ("handoff_confirmed", "observe_only"),
        ORPHANED: ("expired_reservation", "observe_only"),
        LOST_TERMINAL: ("terminal_loss", "keep"),
    }
    assert (exited.kind, exited.action) == ("exited", "observe_only")
    assert world.rows() == before

    enforced = world.reconciler.reconcile()

    assert kinds(enforced)[LOST_TERMINAL] == ("orphan_occupancy", "release")
    assert sorted(world.rows()) == [SUCCESSOR]


def test_a_row_that_changed_after_judgement_is_kept(world):
    world.scene()
    world.clock[0] += TTL
    real = world.reconciler.capacity.drop

    def raced(account, expected):
        world.store.redis.hset(
            f"{ROOT}:accounts:{ACCOUNT}", ORPHANED, json.dumps({**world.row(ORPHANED), "expires_ms": 10**12})
        )
        return real(account, expected)

    world.reconciler.capacity.drop = raced
    found = world.reconciler.reconcile()

    assert kinds(found)[ORPHANED] == ("expired_reservation", "keep")
    assert kinds(found)[SUCCESSOR] == ("expired_reservation", "release")
    assert sorted(world.rows()) == sorted([LOST_TERMINAL, ORPHANED, RETIRING])


def test_other_swarms_rows_are_never_judged(world):
    world.scene()
    foreign = {**world.row(ORPHANED), "holder": "elsewhere/eng-2@elsewhere"}
    world.store.redis.hset(f"{ROOT}:accounts:{ACCOUNT}", foreign["holder"], json.dumps(foreign))
    world.clock[0] += TTL

    found = world.reconciler.reconcile()

    assert "elsewhere/eng-2@elsewhere" not in kinds(found)
    assert "elsewhere/eng-2@elsewhere" in world.rows()


def test_a_superseded_predecessor_cannot_release_its_retiring_row_or_the_newer_slot(world):
    world.running(HANDOFF)
    old = world.tokens[HANDOFF]
    _, token = world.launch(HANDOFF)
    world.capacity.reserve(token, CAP, TTL)
    before = world.rows()

    with pytest.raises(SwarmError):
        world.capacity.release(old)

    assert sorted(before) == sorted([RETIRING, SUCCESSOR])
    assert world.rows() == before


def test_a_replacement_of_a_closed_occupancy_takes_the_seat_slot(world):
    world.running(HANDOFF)
    agent = world.agents[HANDOFF]
    world.fleet.close(MACHINE, world.session(agent).session_id, world.tokens[HANDOFF])
    _, token = world.launch(HANDOFF)

    slot = world.capacity.reserve(token, CAP, TTL)

    assert (slot.holder, slot.generation) == (SUCCESSOR, 2)
    assert sorted(world.rows()) == [SUCCESSOR]


def test_reconstruct_keeps_a_live_predecessor_beside_its_confirmed_successor(world):
    world.running(HANDOFF)
    predecessor = world.agents[HANDOFF]
    grants = {predecessor.execution_id: world.authority.authorize(world.tokens[HANDOFF])}
    successor, token = world.launch(HANDOFF)
    world.capacity.reserve(token, CAP, TTL)
    world.confirm(successor, token)
    grants[successor.execution_id] = world.authority.authorize(token)
    world.store.redis.delete(f"{ROOT}:accounts:{ACCOUNT}")

    rebuilt = world.capacity.reconstruct(world.fleet, grants.get)

    stored = {name: json.loads(raw) for name, raw in world.rows().items()}
    assert rebuilt == {ACCOUNT: 2}
    assert {name: (row["execution_id"], row["generation"], row["state"]) for name, row in stored.items()} == {
        RETIRING: (predecessor.execution_id, 1, OCCUPIED),
        SUCCESSOR: (successor.execution_id, 2, OCCUPIED),
    }


def test_status_reports_the_reconciliation_and_releases_nothing(world, monkeypatch):
    from scripts.swarm import status

    world.scene()
    world.clock[0] += TTL
    monkeypatch.setattr(status, "page_quota", lambda: {})
    monkeypatch.delenv(reconciliation.MODE, raising=False)
    before = world.rows()

    block = status.status_report(world.store, SLUG, {"tasks": []})["account_reconciliation"]

    assert block["discrepancies"] == 1
    assert {(row["holder"], row["kind"], row["action"]) for row in block["findings"]} == {
        (RETIRING, "handoff_unconfirmed", "keep"),
        (SUCCESSOR, "expired_reservation", "release"),
        (ORPHANED, "expired_reservation", "release"),
        (LOST_TERMINAL, "terminal_loss", "keep"),
    }
    assert block["mode"] == "enforce"
    assert world.rows() == before


def test_holds_names_only_the_exact_execution_and_generation_of_a_row(world):
    world.running(TERMINAL)
    lost = world.agents[TERMINAL]

    assert world.reconciler.holds(lost.execution_id, lost.generation) is True
    assert world.reconciler.holds(lost.execution_id, lost.generation + 1) is False
    assert world.reconciler.holds("exec-other", lost.generation) is False
    world.observed(lost, "lost_terminal", sources="supervisor_exit")
    world.reconciler.exited(lost.execution_id, lost.generation, "supervisor")
    assert world.reconciler.holds(lost.execution_id, lost.generation) is False


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        (("pod_failed",), "kubernetes"),
        (("supervisor_exit",), "supervisor"),
        (("supervisor_exit", "pod_failed"), "kubernetes"),
        (("pod_running",), None),
        (("pod_gone",), None),
        (("pod_unreachable",), None),
        (("pod_failed_forbidden",), None),
        ((), None),
    ],
)
def test_the_exit_source_is_the_first_source_whose_reading_shows_an_exit(sources, expected):
    cases = {
        **INPUTS,
        "pod_failed_forbidden": {"kubernetes": {**INPUTS["pod_failed"]["kubernetes"], "reading": "forbidden"}},
    }
    found = {name: entry for case in sources for name, entry in cases[case].items()}
    values = INPUTS["lost_worker"]
    seen = observe.Classification(
        "exec-1",
        1,
        observe.State(values["state"]),
        observe.Terminal(values["terminal"]),
        observe.Failure(values["failure"]),
        observe.Confidence(values["confidence"]),
        1.0,
        1.0,
        1.0,
        found,
    )

    assert reconciliation.exit_source(seen) == expected
