from dataclasses import replace
from types import SimpleNamespace

import pytest

from scripts.swarm import capacity
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.tick import tick
from tests.swarm.profile_fixture import validated
from tests.swarm.test_tick import FakeRuntime, store, tasks, workers  # noqa: F401

pytestmark = pytest.mark.xdist_group("fakeredis")

LAUNCH = {"profile": "engineer", "harness": "claude", "model": "opus", "effort": "high", "account": "old"}


class QuotaRuntime(FakeRuntime):
    def __init__(self, home, pool, monkeypatch):
        super().__init__()
        self.argv, self.chosen = [], []
        monkeypatch.setattr(capacity, "accounts", lambda environ, now, refresh=True: list(pool))
        self.herdr = HerdrRuntime(home=home, run=self._run, choose=self._choose, herdr=self._herdr)

    def _choose(self, requested, environ):
        self.chosen.append(requested)
        return requested, "requested"

    def _herdr(self, args):
        raise AssertionError(f"unexpected herdr call {args}")

    def _run(self, argv, **kwargs):
        self.argv.append(argv)
        return SimpleNamespace(returncode=0, stdout=validated(argv, "status=started\nroute_status=routed\n"), stderr="")

    def has_capacity(self, config):
        return self.herdr.has_capacity(config)

    def quota_capacity(self, *args, **kwargs):
        return self.herdr.quota_capacity(*args, **kwargs)

    def quota_requirements(self, config, ready):
        return self.herdr.quota_requirements(config, ready)

    def spawn(self, config, lane, name, task):
        if not task.get("handoff"):
            return super().spawn(config, lane, name, task)
        placed = self.herdr.spawn(config, lane, name, task)
        self.live.add(name)
        self.spawned.append((lane, name, task["id"]))
        self.tasks.append(dict(task))
        return placed


def _ledger():
    ledger = tasks(("t1", "eng"), ("t2", "eng"))
    ledger.comments = []
    ledger.comment = lambda slug, task, text, by: ledger.comments.append((task, text))
    return ledger


def _quota_handoff(store, ledger, runtime):  # noqa: F811
    for task in ledger.rows.values():
        task["title"] = "keep the seat"
    tick("sw", store, ledger, runtime, 1)
    done, first = sorted(workers(store), key=lambda agent: agent.seat)
    assert (done.seat, first.seat) == ("eng-1@sw", "eng-2@sw")
    ledger.rows[done.task].update(state="done", done=True)
    envelope = {"reason": "quota", "agent": first.name, "task": first.task, "launch": LAUNCH}
    store.put_handoff("sw", first.task, "handoff document", seat=first.seat, envelope=envelope)
    for agent in (done, first):
        store.put_agent("sw", replace(agent, state="finished"))
    return done, first, envelope


def _capacity(eng, claude, codex):
    return (
        f"quota capacity eng {eng} ci 0 plan 0 because accounts have quota; "
        f"Claude has {claude} free seats and Codex has {codex} free seats"
    )


def _option(argv, flag):
    return argv[argv.index(flag) + 1]


OLD = capacity.Account("claude", "old", "OPEN", 0, 5, 90, 2)
FRESH = capacity.Account("claude", "fresh", "OPEN", 1, 90, 90, 6)


def test_a_quota_handoff_keeps_the_seat_and_launch_settings_on_another_account(store, tmp_path, monkeypatch):  # noqa: F811
    pool = [OLD, FRESH, capacity.Account("codex", "roomy", "OPEN", 0, 100, 100, 6)]
    ledger, runtime = _ledger(), QuotaRuntime(tmp_path, pool, monkeypatch)
    done, first, envelope = _quota_handoff(store, ledger, runtime)
    actions = tick("sw", store, ledger, runtime, 2)
    (argv,) = runtime.argv
    successor = next(a for a in workers(store) if a.name != first.name)
    assert actions == [
        f"retired {done.name}",
        f"retired {first.name}",
        _capacity(1, 7, 6),
        f"spawned {successor.name} for {first.task}",
    ]
    assert successor.seat == first.seat
    assert runtime.chosen == ["claude"]
    assert runtime.tasks[-1]["handoff_envelope"] == envelope
    assert _option(argv, "--route") == "fresh"
    assert [_option(argv, flag) for flag in ("--profile", "--agent", "--model", "--effort")] == [
        "engineer",
        "claude",
        "opus",
        "high",
    ]
    assert store.handoff("sw", first.task) == ""


def _waits_with_its_handoff(store, ledger, runtime, first, envelope):  # noqa: F811
    assert runtime.argv == []
    assert ledger.rows[first.task]["state"] == "open"
    assert (store.handoff("sw", first.task), store.handoff_seat("sw", first.task)) == ("handoff document", first.seat)
    assert store.handoff_envelope("sw", first.task) == envelope


def test_a_quota_handoff_waits_with_its_handoff_when_no_other_account_qualifies(store, tmp_path, monkeypatch):  # noqa: F811
    pool = [OLD]
    ledger, runtime = _ledger(), QuotaRuntime(tmp_path, pool, monkeypatch)
    done, first, envelope = _quota_handoff(store, ledger, runtime)
    actions = tick("sw", store, ledger, runtime, 2)
    assert actions == [
        f"retired {done.name}",
        f"retired {first.name}",
        _capacity(1, 2, 0),
        f"spawn failed for {first.task}, task {first.task} reopened: no claude account has placeable quota seats",
    ]
    _waits_with_its_handoff(store, ledger, runtime, first, envelope)
    pool.append(FRESH)
    tick("sw", store, ledger, runtime, 3)
    (argv,) = runtime.argv
    (successor,) = (a for a in workers(store) if a.task == first.task and a.name != first.name)
    assert _option(argv, "--route") == "fresh"
    assert successor.seat == first.seat


def test_a_quota_handoff_waits_with_its_handoff_while_every_account_is_full(store, tmp_path, monkeypatch):  # noqa: F811
    pool = [OLD]
    ledger, runtime = _ledger(), QuotaRuntime(tmp_path, pool, monkeypatch)
    done, first, envelope = _quota_handoff(store, ledger, runtime)
    pool[0] = replace(OLD, sessions=2)
    actions = tick("sw", store, ledger, runtime, 2)
    assert actions == [f"retired {done.name}", f"retired {first.name}", _capacity(0, 0, 0)]
    _waits_with_its_handoff(store, ledger, runtime, first, envelope)
