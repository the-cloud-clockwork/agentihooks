import json
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from scripts.swarm import recovery_view as rv
from scripts.swarm import resume
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2.checkpoints import model

pytestmark = pytest.mark.unit

SLUG = "rv"
NOW = 1_000_000


def material(**changes):
    base = {
        "execution_id": "exe-1",
        "generation": 1,
        "agent": "eng-7",
        "seat": f"eng-1@{SLUG}",
        "task": "t1",
        "reason": "node_lost",
        "fenced_at_ms": NOW - 61_000,
        "archive_watermark": 4096,
        "tail": "tail text",
        "checkpoint": {"checkpoint_id": "ck-2", "sequence": 2, "continuation": "native"},
        "refused": [],
        "choices": {"native": "", "context": "", "fresh": ""},
        "decision": {"mode": rv.PENDING, "replacement": ""},
        "decided_at_ms": 0,
        "requested": "",
        "refused_request": None,
    }
    return {**base, **changes}


def test_names_are_fixed():
    assert rv.MATERIAL == "attempt-recovery-material"
    assert rv.METRIC == "recovery_decision_wait_seconds"
    assert (rv.RECOVERABLE, rv.CONTEXT_ONLY, rv.FRESH_ONLY, rv.PATH_ONLY) == (
        "recoverable",
        "context only",
        "fresh only",
        "path only",
    )
    assert rv.CHOICES == ("native", "context", "fresh")
    assert rv.LABELS == {
        "native": "native resume",
        "context": "context continuation",
        "fresh": "fresh start",
        "resume": "resume",
    }
    assert rv.ACCEPTED == "a recovery decision was already accepted"
    assert rv.REQUESTED == "a request is waiting for the controller's next pass"
    assert rv.NO_CHECKPOINT == "no complete compatible checkpoint"
    assert rv.NO_WATERMARK == "a local snapshot records no transcript archive watermark"


def test_a_native_checkpoint_card_is_recoverable_with_every_choice_open():
    card = rv.pod_card(material(), NOW)
    assert card == {
        "seat": f"eng-1@{SLUG}",
        "agent": "eng-7",
        "task": "t1",
        "backend": "kubernetes",
        "reason": "node lost",
        "badge": rv.RECOVERABLE,
        "material": ["ck-2 · sequence 2 · native", "archive watermark 4096", "tail text"],
        "diagnosis": ["native resume: available"],
        "choices": [
            {"choice": "native", "label": "native resume", "send": "native", "unavailable": ""},
            {"choice": "context", "label": "context continuation", "send": "context", "unavailable": ""},
            {"choice": "fresh", "label": "fresh start", "send": "fresh", "unavailable": ""},
        ],
        "requested": "",
        "requested_text": "",
        "accepted": None,
        "wait_seconds": 61,
    }


def test_a_context_only_card_explains_the_native_failure_and_names_its_limit():
    refused = [{"checkpoint_id": "ck-3", "sequence": 3, "diagnosis": "corrupt_object"}]
    choices = {"native": "no native", "context": "", "fresh": ""}
    card = rv.pod_card(
        material(choices=choices, refused=refused, refused_request={"choice": "native", "reason": "no"}), NOW
    )
    assert card["badge"] == rv.CONTEXT_ONLY
    assert card["diagnosis"] == [
        "native resume: no native",
        "passed over ck-3: corrupt_object",
        *model.CONTEXT_LIMITS,
        "refused native resume: no",
    ]
    assert [c["unavailable"] for c in card["choices"]] == ["no native", "", ""]


def test_without_any_checkpoint_only_fresh_remains():
    choices = {"native": "a", "context": "b", "fresh": ""}
    card = rv.pod_card(material(choices=choices, checkpoint=None), NOW)
    assert (card["badge"], card["material"][0]) == (rv.FRESH_ONLY, rv.NO_CHECKPOINT)
    assert card["diagnosis"] == ["native resume: a"]
    assert card["requested_text"] == ""


def test_a_waiting_request_holds_every_choice_and_names_itself():
    card = rv.pod_card(material(requested="context"), NOW)
    assert card["requested"] == "context"
    assert card["requested_text"] == "requested: context continuation"
    assert {c["unavailable"] for c in card["choices"]} == {rv.REQUESTED}


def test_an_accepted_decision_hides_the_request_and_stops_the_clock():
    decision = {"mode": "context", "replacement": "exe-2"}
    card = rv.pod_card(material(decision=decision, requested="context", decided_at_ms=NOW - 1_000), NOW + 500_000)
    assert card["accepted"] == {"choice": "context", "replacement": "exe-2", "text": "accepted: context continuation"}
    assert (card["requested"], card["wait_seconds"]) == ("", 60)
    assert {c["unavailable"] for c in card["choices"]} == {rv.ACCEPTED}


def test_a_decision_without_a_recorded_time_waits_until_now():
    decision = {"mode": "resume", "replacement": "exe-2"}
    card = rv.pod_card(material(decision=decision), NOW)
    assert (card["wait_seconds"], card["accepted"]["text"]) == (61, "accepted: resume")
    assert rv.pod_card(material(fenced_at_ms=NOW + 5), NOW)["wait_seconds"] == 0


@pytest.fixture
def store():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, ".", 3, 0, state="running"))
    return store


def _pod(store, seat="eng-1", **changes):
    target = {"pod_namespace": "w", "pod_name": "p", "pod_uid": "u"}
    agent = AgentRecord(store.next_name(SLUG, "eng"), "eng", "t1", seat=f"{seat}@{SLUG}")
    agent = replace(agent, runtime_backend="kubernetes", runtime_target=target, **changes)
    return store.start_execution(SLUG, agent)


def _successor(old):
    target = {"pod_namespace": "w", "pod_name": "p2", "pod_uid": "u2"}
    return replace(old, execution_id="", generation=0, runtime_target=target)


def _publish(store, shown):
    store.redis.hset(store.key(SLUG, rv.MATERIAL), shown["execution_id"], json.dumps(shown))


def test_the_view_shows_only_the_current_attempt_of_each_seat_with_the_live_request(store):
    old = _pod(store)
    newer = store.start_execution(SLUG, _successor(old), old.execution_id)
    _publish(store, material(execution_id=old.execution_id, seat=old.seat, generation=1))
    _publish(store, material(execution_id=newer.execution_id, seat=old.seat, generation=2, agent="eng-8"))
    _publish(store, material(execution_id="exe-gone", seat="eng-9@rv", generation=1))
    store.redis.hset(store.key(SLUG, resume.RECOVERY_REQUESTS), newer.execution_id, "fresh")
    shown = rv.view(store, SLUG, NOW)
    assert [(c["agent"], c["requested"]) for c in shown["cards"]] == [("eng-8", "fresh")]
    assert shown[rv.METRIC] == {old.seat: 61}


def test_the_replacement_keeps_its_fenced_attempt_card(store):
    old = _pod(store)
    newer = store.start_execution(SLUG, _successor(old), old.execution_id)
    decision = {"mode": "native", "replacement": newer.execution_id}
    _publish(store, material(execution_id=old.execution_id, seat=old.seat, decision=decision))
    [card] = rv.view(store, SLUG, NOW)["cards"]
    assert card["accepted"]["replacement"] == newer.execution_id


def _local(store, state=resume.AWAITING, **changes):
    agent = AgentRecord("eng-local", "eng", "t9", seat=f"eng-5@{SLUG}", conversation_id="c-1", state=state)
    store.put_agent(SLUG, replace(agent, **changes))
    return agent


def _restored(store, **changes):
    row = asdict(resume.Outcome("eng-local", "eng", "t9", resume.AWAITING, "worktree gone", at=NOW - 30_000))
    store.put_restored(SLUG, [{**row, **changes}])


def test_a_local_restore_is_path_only_and_never_offers_context(store):
    _local(store)
    _restored(store)
    [card] = rv.view(store, SLUG, NOW)["cards"]
    assert card["badge"] == rv.PATH_ONLY
    assert card["material"] == [rv.NO_CHECKPOINT, rv.NO_WATERMARK]
    assert card["diagnosis"] == ["native resume: worktree gone", *model.PATH_ONLY_LIMITS]
    assert [(c["send"], c["unavailable"]) for c in card["choices"]] == [
        ("native", "worktree gone"),
        ("", resume.LOCAL_CONTEXT),
        ("fresh", ""),
    ]
    assert (card["backend"], card["seat"], card["wait_seconds"], card["accepted"]) == (
        "local",
        f"eng-5@{SLUG}",
        30,
        None,
    )


def test_a_retryable_local_failure_keeps_native_resume_open(store):
    _local(store, seat="")
    _restored(store, reason="resume failed to start: busy")
    [card] = rv.view(store, SLUG, NOW)["cards"]
    assert card["seat"] == "eng-local"
    assert card["choices"][0]["unavailable"] == ""
    assert card["diagnosis"][0] == f"native resume: {rv.LOCAL_NATIVE}"


@pytest.mark.parametrize("reason", ["no conversation id", "no worktree", "worktree gone"])
def test_each_lost_local_conversation_or_worktree_disables_native(store, reason):
    _local(store)
    _restored(store, reason=reason)
    [card] = rv.view(store, SLUG, NOW)["cards"]
    assert card["choices"][0] == {"choice": "native", "label": "native resume", "send": "native", "unavailable": reason}


def test_a_decided_local_restore_keeps_its_accepted_choice(store):
    _local(store, state="working")
    _restored(store, outcome=resume.RESUMED, decided=True, at=NOW - 1_000, awaited_at=NOW - 31_000)
    [card] = rv.view(store, SLUG, NOW)["cards"]
    assert card["accepted"] == {"choice": "native", "replacement": "", "text": "accepted: native resume"}
    assert card["wait_seconds"] == 30
    assert {c["unavailable"] for c in card["choices"]} == {rv.ACCEPTED}


def test_undecided_or_unknown_local_rows_and_pod_agents_are_not_shown(store):
    _local(store, state="working")
    _restored(store, outcome=resume.RESUMED)
    assert rv.view(store, SLUG, NOW)["cards"] == []
    pod = _pod(store, seat="eng-6")
    store.put_agent(SLUG, replace(pod, state=resume.AWAITING))
    store.put_restored(SLUG, [asdict(resume.Outcome(pod.name, "eng", "t1", resume.AWAITING, "x"))])
    assert rv.view(store, SLUG, NOW)["cards"] == []
    store.put_restored(SLUG, [asdict(resume.Outcome("nobody", "eng", "t", resume.AWAITING, "x"))])
    assert rv.view(store, SLUG, NOW) == {"cards": [], rv.METRIC: {}}


@pytest.mark.parametrize("module", ["scripts.swarm.recovery_view", "scripts.swarm.resume"])
def test_the_recovery_modules_import_first_in_a_fresh_interpreter(module):
    root = Path(__file__).parents[2]
    result = subprocess.run([sys.executable, "-c", f"import {module}"], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_wait_seconds_round_down_and_never_go_negative():
    assert [rv._seconds(0, 1_999), rv._seconds(5, 5), rv._seconds(10, 5)] == [1, 0, 0]


def test_a_card_without_a_decision_record_is_still_pending():
    card = rv.pod_card(material(decision=None), NOW)
    assert (card["accepted"], card["choices"][0]["unavailable"], card["wait_seconds"]) == (None, "", 61)


def test_a_local_card_names_its_agent_task_and_reason_and_never_a_request(store):
    _local(store)
    _restored(store)
    [card] = rv.view(store, SLUG, NOW)["cards"]
    shown = {key: card[key] for key in ("seat", "agent", "task", "reason", "requested", "requested_text")}
    assert shown == {
        "seat": f"eng-5@{SLUG}",
        "agent": "eng-local",
        "task": "t9",
        "reason": "worktree gone",
        "requested": "",
        "requested_text": "",
    }


def test_local_cards_are_ordered_by_seat(store):
    for name, seat in (("eng-b", "eng-9"), ("eng-a", "eng-2")):
        agent = AgentRecord(name, "eng", "t9", seat=f"{seat}@{SLUG}", conversation_id="c", state=resume.AWAITING)
        store.put_agent(SLUG, agent)
    names = ("eng-b", "eng-a")
    store.put_restored(
        SLUG, [asdict(resume.Outcome(n, "eng", "t9", resume.AWAITING, "worktree gone", at=NOW)) for n in names]
    )
    assert [card["seat"] for card in rv.view(store, SLUG, NOW)["cards"]] == [f"eng-2@{SLUG}", f"eng-9@{SLUG}"]


def test_the_swarm_view_carries_the_recovery_projection_at_the_current_time(store, monkeypatch):
    from scripts.swarm import commands

    _local(store)
    _restored(store)
    monkeypatch.setattr(commands.time, "time_ns", lambda: NOW * 1_000_000)
    shown = commands.view(store, SLUG)["recovery"]
    assert shown == rv.view(store, SLUG, NOW)
    assert shown[rv.METRIC] == {f"eng-5@{SLUG}": 30}
