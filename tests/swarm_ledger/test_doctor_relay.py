import pytest

from hooks.context import master_coding, operator_words
from scripts.swarm_ledger import ledger_relay
from tests.swarm_ledger.test_ledger_relay import _cli, core, make_ledger

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def doctor(monkeypatch):
    import fakeredis

    from scripts.swarm.store import RedisStore, SwarmConfig

    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("hooks._redis.get_redis", lambda: redis)
    store = RedisStore(redis)
    for slug, code, template in (("watched", "a1", ""), ("doctor", "b2", "doctor"), ("other", "c3", "")):
        store.names.adopt(slug, code, slug, "/repo")
        store.create(SwarmConfig(slug, "/repo", 0, 0, template=template))
    store.set_peer("doctor", "watched")
    store.set_peer("watched", "doctor")
    return store


def test_doctor_relays_words_typed_in_the_watched_master_pane(doctor, monkeypatch, capsys):
    state = make_ledger()
    core.sync(
        "relay-2026-01-01", ops=[{"op": "join", "id": "doctor-join", "by": "master@b2-1", "role": "orchestrator"}]
    )
    question = f"questions/{state['questions'][0]['id']}"
    master_coding.record_seat("watched-pane", "watched", "master@a1-1")
    assert operator_words.typed({"session_id": "watched-pane", "prompt": "Fix only swarm health"}, {})
    _cli(monkeypatch, question, "fix only swarm health", by="master@b2-1")
    assert '"relayed": true' in capsys.readouterr().out
    state, _ = core.sync("relay-2026-01-01")
    assert state["questions"][0]["answers"][-1]["quote"] == "Fix only swarm health"


@pytest.mark.parametrize("speaker", ["master@c3-1", "engineer@a1-1", "planner@a1-1"])
def test_doctor_refuses_quotes_from_unrelated_or_nonmaster_panes(doctor, speaker):
    operator_words.record(speaker, "Fix only swarm health")
    assert ledger_relay.verified("master@b2-1", "Fix only swarm health") == ""


def test_watched_swarm_cannot_borrow_its_doctors_operator_quotes(doctor):
    operator_words.record("master@b2-1", "Fix only swarm health")
    assert ledger_relay.verified("master@a1-1", "Fix only swarm health") == ""


def test_doctor_cannot_borrow_watched_quotes_after_unlinking(doctor):
    operator_words.record("master@a1-1", "Fix only swarm health")
    doctor.clear_peer("doctor")
    assert ledger_relay.verified("master@b2-1", "Fix only swarm health") == ""


def test_doctor_quote_sources_are_only_recorded_watched_masters(doctor):
    for speaker in ("master@a1-1", "master@a1-2", "planner@a1-1", "engineer@a1-1", "master@c3-1"):
        operator_words.record(speaker, "Fix only swarm health")
    assert sorted(ledger_relay.watched_masters("master@b2-1")) == ["master@a1-1", "master@a1-2"]
    assert ledger_relay.verified("master@b2-1", "fix ONLY swarm health") == "Fix only swarm health"
    assert ledger_relay.verified("master@b2-1", "words never typed") == ""
    assert (
        ledger_relay.relayed(
            {"by": "engineer@b2-1", "quote": "Fix only swarm health"},
            {"members": {"engineer@b2-1": {"role": "member"}}},
        )
        == ""
    )


def test_quote_sources_remain_local_without_redis(monkeypatch):
    monkeypatch.setattr("hooks._redis.get_redis", lambda: None)
    assert ledger_relay.watched_masters("master@b2-1") == []


def test_doctor_with_an_unregistered_peer_has_no_watched_master_sources(doctor):
    doctor.set_peer("doctor", "unregistered")
    operator_words.record("master@-1", "Fix only swarm health")
    assert ledger_relay.watched_masters("master@b2-1") == []
