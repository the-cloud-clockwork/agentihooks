import json
from types import SimpleNamespace

import pytest

from scripts.swarm import profile_choice, profile_first
from scripts.swarm.store import RedisStore, SwarmConfig

PLACED = SimpleNamespace(router=SimpleNamespace(placement=object()))
DECISION = profile_choice.ProfileDecision("frontend", "classifier", "frontend", "m1", 0.9, True, ("task:t1",))
ENGINEER = profile_choice.ProfileDecision("engineer", "classifier", "engineer", "m1", 0.8, True)
LAUNCH = {"profile": "qa", "harness": "claude", "model": "opus", "effort": "high", "overlays": ["o1"]}
KEPT = profile_choice.ProfileDecision("qa", "handoff", "original seat profile", overlays=("o1",)).record()
EXPLICIT = profile_choice.ProfileDecision("frontend", "task", "explicit task profile").record()


@pytest.fixture
def store():
    import fakeredis

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig("sw", ".", 2, 1))
    return saved


@pytest.fixture
def asked(monkeypatch):
    calls = []

    def classify(slug, task, environ):
        calls.append((slug, task["id"], environ))
        return ENGINEER if task["id"].startswith("e") else DECISION

    monkeypatch.setattr(profile_choice, "classify", classify)
    monkeypatch.setenv("PROFILE_FIRST_PROBE", "seen")
    return calls


def decided(store, task, lane="eng", runtime=PLACED):
    return profile_first.decided("sw", store.config("sw"), store, runtime, lane, task)


def cached(store):
    return store.redis.hgetall(store.key("sw", profile_first.KEY))


def handed_off(store, task_id, launch):
    store.redis.set(store.key("sw", "handoff-envelope", task_id), json.dumps({"reason": "context", "launch": launch}))


def relaunched(store, task_id, assignment):
    store.redis.hset(store.key("sw", "launch-assignments"), task_id, json.dumps(assignment))


def test_a_classified_decision_is_recorded_on_the_task_and_kept_until_the_task_spawns(store, asked):
    task = {"id": "t1"}
    assert decided(store, task) is task
    assert task == {"id": "t1", "profile_decision": DECISION.record()}
    assert [(slug, task_id) for slug, task_id, _ in asked] == [("sw", "t1")]
    assert asked[0][2]["PROFILE_FIRST_PROBE"] == "seen"
    assert cached(store) == {"t1": json.dumps(DECISION.record())}
    assert decided(store, {"id": "t1"}) == {"id": "t1", "profile_decision": DECISION.record()}
    assert len(asked) == 1
    profile_first.spawned("sw", store, "t1")
    assert cached(store) == {}


def test_a_lane_decision_is_recorded_without_the_classifier_and_never_kept(store, asked):
    task = decided(store, {"id": "g1"}, lane="ci")
    assert task["profile_decision"] == profile_choice.ProfileDecision("cicd", "lane", "ci lane").record()
    assert asked == []
    assert cached(store) == {}


def test_a_lane_pinned_to_a_profile_decides_it_without_the_classifier(store, asked):
    config = SimpleNamespace(lanes={"eng": {"profile": "qa"}})
    task = profile_first.decided("sw", config, store, PLACED, "eng", {"id": "t1"})
    assert task["profile_decision"] == profile_choice.ProfileDecision("qa", "lane", "eng lane").record()
    assert asked == []


def test_an_explicit_profile_is_recorded_as_the_decision_without_the_classifier(store, asked):
    task = decided(store, {"id": "t1", "profile": "frontend"})
    assert (
        task["profile_decision"] == profile_choice.ProfileDecision("frontend", "task", "explicit task profile").record()
    )
    assert asked == []
    assert cached(store) == {}


def test_an_explicit_profile_set_after_a_cached_answer_wins(store, asked):
    store.redis.hset(store.key("sw", profile_first.KEY), "e1", json.dumps(ENGINEER.record()))
    task = decided(store, {"id": "e1", "profile": "frontend"})
    assert task["profile_decision"] == EXPLICIT
    assert asked == []


@pytest.mark.parametrize(
    "runtime",
    [SimpleNamespace(router=SimpleNamespace(placement=None)), SimpleNamespace(router=None), SimpleNamespace()],
)
def test_an_unplaced_runtime_decides_nothing(store, asked, runtime):
    assert decided(store, {"id": "t1"}, runtime=runtime) == {"id": "t1"}
    assert asked == []
    assert cached(store) == {}


@pytest.mark.parametrize("error", [profile_choice.ProfileUnresolved("mixed"), RuntimeError("ledger down")])
def test_an_undecided_task_is_left_to_the_spawn_path(store, monkeypatch, error):
    def fails(slug, task, environ):
        raise error

    monkeypatch.setattr(profile_choice, "classify", fails)
    assert decided(store, {"id": "t1"}) == {"id": "t1"}
    assert cached(store) == {}


def test_a_handoff_keeps_the_original_seat_profile_ahead_of_a_cached_answer(store, asked):
    store.redis.hset(store.key("sw", profile_first.KEY), "t1", json.dumps(DECISION.record()))
    handed_off(store, "t1", LAUNCH)
    assert decided(store, {"id": "t1"})["profile_decision"] == KEPT
    assert decided(store, {"id": "t1", "profile": "frontend"})["profile_decision"] == (
        profile_choice.ProfileDecision("frontend", "task", "explicit task profile").record()
    )
    assert asked == []


def test_a_complete_relaunch_record_wins_over_an_explicit_profile_and_the_handoff(store, asked):
    handed_off(store, "t1", {**LAUNCH, "profile": "engineer"})
    relaunched(store, "t1", {**LAUNCH, "seat": "eng-1@sw"})
    assert decided(store, {"id": "t1", "profile": "frontend"})["profile_decision"] == KEPT
    assert asked == []


def test_an_incomplete_relaunch_record_falls_back_to_the_handoff_launch(store, asked):
    handed_off(store, "t1", LAUNCH)
    relaunched(store, "t1", {**LAUNCH, "effort": ""})
    assert decided(store, {"id": "t1"})["profile_decision"] == KEPT
    relaunched(store, "t2", {**LAUNCH, "model": ""})
    assert decided(store, {"id": "t2"})["profile_decision"] == DECISION.record()


def refusing_frontend(lane, task):
    local = lane != "eng" or task["profile_decision"]["profile"] == "frontend"
    return f"the workstation hive spawns {lane} work" if local else ""


@pytest.mark.parametrize(
    ("room", "taken", "seen", "waits"),
    [
        (0, [], [], []),
        (1, ["e1"], ["f1", "e1"], ["task f1 waits: the workstation hive spawns eng work"]),
        (2, ["e1", "e2"], ["f1", "e1", "e2"], ["task f1 waits: the workstation hive spawns eng work"]),
        (5, ["e1", "e2"], ["f1", "e1", "e2"], ["task f1 waits: the workstation hive spawns eng work"]),
    ],
)
def test_placeable_decides_ready_tasks_only_until_the_free_slots_fill(store, asked, room, taken, seen, waits):
    runtime = SimpleNamespace(router=SimpleNamespace(placement=object()), placement_refusal=refusing_frontend)
    ready = [{"id": "f1"}, {"id": "e1"}, {"id": "e2"}]
    found, waited = profile_first.placeable("sw", store.config("sw"), store, runtime, "eng", ready, room)
    assert [task["id"] for task in found] == taken
    assert waited == waits
    assert [task_id for _, task_id, _ in asked] == seen


def unresolved_u(lane, task):
    if task["id"].startswith("u"):
        raise profile_choice.ProfileUnresolved(f"{task['id']} profile is unresolved")
    return ""


@pytest.mark.parametrize(
    ("room", "taken"),
    [(0, []), (1, ["u1", "e1"]), (2, ["u1", "e1", "u2", "e2"])],
)
def test_placeable_marks_an_unresolved_task_without_spending_a_slot(store, asked, room, taken):
    runtime = SimpleNamespace(router=SimpleNamespace(placement=object()), placement_refusal=unresolved_u)
    ready = [{"id": "u1"}, {"id": "e1"}, {"id": "u2"}, {"id": "e2"}, {"id": "e3"}]
    found, waited = profile_first.placeable("sw", store.config("sw"), store, runtime, "eng", ready, room)
    assert [task["id"] for task in found] == taken
    assert {task["id"]: task.get(profile_first.UNRESOLVED) for task in found} == {
        task_id: f"{task_id} profile is unresolved" if task_id.startswith("u") else None for task_id in taken
    }
    assert waited == []


def test_placeable_lets_any_other_placement_error_through(store, asked):
    def broken(lane, task):
        raise RuntimeError("hive registry down")

    runtime = SimpleNamespace(router=SimpleNamespace(placement=object()), placement_refusal=broken)
    with pytest.raises(RuntimeError, match="^hive registry down$"):
        profile_first.placeable("sw", store.config("sw"), store, runtime, "eng", [{"id": "e1"}], 1)
