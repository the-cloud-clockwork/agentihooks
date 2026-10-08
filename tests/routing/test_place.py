import pytest

from scripts.routing import place, settings
from scripts.routing.slots import API, API_UNBOUNDED, Slot

NOW = 1_900_000_000.0


def _api(sessions=0, cap=API_UNBOUNDED):
    return Slot("claude", "api", cap, sessions, kind=API, provider="gateway")


def _pool(sessions, caps=None):
    caps = caps or {}
    return [Slot("claude", name, caps.get(name, 6), sessions.get(name, 0)) for name in ("A", "B", "C")]


def _launches(count, weight, api_cap=API_UNBOUNDED, caps=None):
    sessions, kinds = {}, []
    for _ in range(count):
        pool = _pool(sessions, caps)
        seat = place.place([_api(sessions.get("api", 0), api_cap)], pool, weight, sum(s.sessions for s in pool))
        if seat is None:
            kinds.append(None)
            continue
        kinds.append(seat.kind)
        sessions[seat.account] = sessions.get(seat.account, 0) + 1
    return kinds, sessions


class _Source:
    def __init__(self, slots):
        self.found = slots

    def slots(self, environ, now):
        return self.found


def test_the_api_cap_is_one_million_so_a_slot_cap_stays_an_int():
    assert API_UNBOUNDED == 1_000_000
    assert type(API_UNBOUNDED) is int


def test_weight_25_gives_api_one_of_every_four_live_sessions():
    kinds, sessions = _launches(8, 25)
    assert kinds == [API, "subscription", "subscription", "subscription"] * 2
    assert sessions == {"api": 2, "A": 2, "B": 2, "C": 2}


def test_weight_0_never_uses_api_while_a_pool_seat_is_free():
    kinds, _ = _launches(6, 0)
    assert kinds == ["subscription"] * 6


def test_a_closed_pool_sends_every_session_to_api_whatever_the_weight():
    kinds, sessions = _launches(4, 0, caps={"A": 0, "B": 0, "C": 0})
    assert kinds == [API] * 4
    assert sessions == {"api": 4}


def test_an_api_at_its_cap_yields_to_the_pool_and_both_closed_places_nothing():
    kinds, sessions = _launches(4, 100, api_cap=1, caps={"A": 1, "B": 1, "C": 0})
    assert kinds == [API, "subscription", "subscription", None]
    assert sessions == {"api": 1, "A": 1, "B": 1}


def test_the_pool_share_counts_the_live_pool_sessions_it_is_given():
    pool = [Slot("claude", "A", 6, 3)]
    assert place.place([_api(1)], pool, 15, 5) == _api(1)
    assert place.place([_api(1)], pool, 14, 5) == pool[0]
    assert place.place([_api(1)], pool, 25, 3) == _api(1)
    assert place.place([_api(1)], pool, 20, 3) == pool[0]


def test_without_an_api_slot_the_pool_pick_is_unchanged():
    pool = [Slot("claude", "A", 6, 2), Slot("claude", "B", 6, 1), Slot("claude", "FULL", 1, 1)]
    assert place.place([], pool, 100, 4) == pool[1]
    assert place.place([], [Slot("claude", "FULL", 1, 1)], 100, 1) is None


def test_api_side_reads_no_policy_without_an_api_slot(monkeypatch):
    monkeypatch.setattr(place, "policy", lambda harness, environ: pytest.fail("policy read without an api slot"))
    assert place.api_side(_Source([]), "claude", {}, NOW) == ([], 0)


def test_api_side_applies_the_harness_policy_to_every_api_slot(monkeypatch):
    seen = []

    def policy(harness, environ):
        seen.append((harness, environ))
        return place.ApiPolicy(25, 3)

    monkeypatch.setattr(place, "policy", policy)
    found = [Slot("codex", "api", 0, 2, kind=API, provider="openai-key")]
    assert place.api_side(_Source(found), "codex", {"K": "v"}, NOW) == (
        [Slot("codex", "api", 3, 2, kind=API, provider="openai-key")],
        25,
    )
    assert seen == [("codex", {"K": "v"})]


def test_policy_defaults_to_weight_zero_and_an_unbounded_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(place, "_client", lambda environ: None)
    assert place.policy("claude", {"AGENTIHOOKS_HOME": str(tmp_path)}) == place.ApiPolicy(0, API_UNBOUNDED)
    assert place.ApiPolicy() == place.ApiPolicy(0, API_UNBOUNDED)


def test_policy_reads_each_harness_weight_and_cap_from_the_store(monkeypatch, tmp_path):
    env = {"AGENTIHOOKS_HOME": str(tmp_path)}
    monkeypatch.setattr(place, "_client", lambda environ: None)
    store = settings.open_store(None, env)
    store.set("claude-api-weight", 25, "operator", NOW)
    store.set("claude-api-max-sessions", 2, "operator", NOW)
    store.set("codex-api-weight", 80, "operator", NOW)
    assert place.policy("claude", env) == place.ApiPolicy(25, 2)
    assert place.policy("codex", env) == place.ApiPolicy(80, API_UNBOUNDED)


@pytest.mark.xdist_group("fakeredis")
def test_policy_reads_the_swarm_redis_when_it_answers(monkeypatch, tmp_path):
    import fakeredis

    from scripts.swarm import store

    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    settings.open_store(fake, {}).set("codex-api-max-sessions", 0, "operator", NOW)
    seen = []
    monkeypatch.setattr(store, "redis_client", lambda environ: seen.append(environ) or fake)
    env = {"AGENTIHOOKS_HOME": str(tmp_path)}
    assert place.policy("codex", env) == place.ApiPolicy(0, 0)
    assert seen == [env]


@pytest.mark.parametrize("error", [ConnectionRefusedError("refused"), ValueError("bad scheme"), "redis"])
def test_an_unreachable_or_misnamed_redis_falls_back_to_the_file_store(monkeypatch, error):
    import redis

    from scripts.swarm import store

    def refused(environ):
        raise redis.ConnectionError("refused") if error == "redis" else error

    monkeypatch.setattr(store, "redis_client", refused)
    assert place._client({}) is None


def test_any_other_redis_client_failure_is_not_swallowed(monkeypatch):
    from scripts.swarm import store

    def broken(environ):
        raise RuntimeError("bug")

    monkeypatch.setattr(store, "redis_client", broken)
    with pytest.raises(RuntimeError):
        place._client({})


def test_an_unreadable_settings_file_is_a_settings_error(monkeypatch, tmp_path):
    monkeypatch.setattr(place, "_client", lambda environ: None)
    (tmp_path / "routing-settings.json").write_text("{not json")
    with pytest.raises(place.SettingsError) as raised:
        place.policy("claude", {"AGENTIHOOKS_HOME": str(tmp_path)})
    assert str(raised.value) == "routing settings are unreadable: JSONDecodeError"
    (tmp_path / "routing-settings.json").write_text("{}")
    with pytest.raises(place.SettingsError) as raised:
        place.policy("claude", {"AGENTIHOOKS_HOME": str(tmp_path)})
    assert str(raised.value) == "routing settings are unreadable: KeyError"


@pytest.mark.xdist_group("fakeredis")
def test_a_redis_read_failure_is_a_settings_error(monkeypatch, tmp_path):
    import redis

    class Failing:
        def hgetall(self, key):
            raise redis.TimeoutError("slow")

    monkeypatch.setattr(place, "_client", lambda environ: Failing())
    with pytest.raises(place.SettingsError) as raised:
        place.policy("codex", {"AGENTIHOOKS_HOME": str(tmp_path)})
    assert str(raised.value) == "routing settings are unreadable: TimeoutError"
