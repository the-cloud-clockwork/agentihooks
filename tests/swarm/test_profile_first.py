from types import SimpleNamespace

import pytest

from scripts.swarm import profile_choice, profile_first
from scripts.swarm.store import RedisStore, SwarmConfig

pytestmark = pytest.mark.xdist_group("fakeredis")

PLACED = SimpleNamespace(router=SimpleNamespace(placement=object()))
DECISION = profile_choice.ProfileDecision("frontend", "classifier", "frontend", "m1", 0.9, True, ("task:t1",))


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
        return DECISION

    monkeypatch.setattr(profile_choice, "classify", classify)
    monkeypatch.setenv("PROFILE_FIRST_PROBE", "seen")
    return calls


def decided(store, task, lane="eng", runtime=PLACED):
    return profile_first.decided("sw", store.config("sw"), store, runtime, lane, task)


def cached(store):
    return store.redis.hgetall(store.key("sw", profile_first.KEY))


def test_a_classified_decision_is_recorded_on_the_task_and_kept_until_the_task_spawns(store, asked):
    task = {"id": "t1"}
    assert decided(store, task) is task
    assert task == {"id": "t1", "profile_decision": DECISION.record()}
    assert [(slug, task_id) for slug, task_id, _ in asked] == [("sw", "t1")]
    assert asked[0][2]["PROFILE_FIRST_PROBE"] == "seen"
    assert decided(store, {"id": "t1"}) == {"id": "t1", "profile_decision": DECISION.record()}
    assert len(asked) == 1
    profile_first.spawned("sw", store, "t1")
    assert cached(store) == {}


def test_a_lane_decision_is_recorded_without_the_classifier_and_never_kept(store, asked):
    task = decided(store, {"id": "g1"}, lane="ci")
    assert task["profile_decision"] == profile_choice.ProfileDecision("cicd", "lane", "ci lane").record()
    assert asked == []
    assert cached(store) == {}


@pytest.mark.parametrize(
    ("task", "runtime"),
    [
        ({"id": "t1", "profile": "qa"}, PLACED),
        ({"id": "t1"}, SimpleNamespace(router=SimpleNamespace(placement=None))),
        ({"id": "t1"}, SimpleNamespace()),
    ],
)
def test_an_explicit_profile_or_an_unplaced_runtime_decides_nothing(store, asked, task, runtime):
    before = dict(task)
    assert decided(store, task, runtime=runtime) == before
    assert asked == []
    assert cached(store) == {}


def test_an_unresolved_profile_is_left_to_the_spawn_path(store, monkeypatch):
    def unresolved(slug, task, environ):
        raise profile_choice.ProfileUnresolved("mixed")

    monkeypatch.setattr(profile_choice, "classify", unresolved)
    assert decided(store, {"id": "t1"}) == {"id": "t1"}
    assert cached(store) == {}
