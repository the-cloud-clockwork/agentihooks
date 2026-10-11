import pytest

from scripts import session_bands
from scripts.swarm.store import RedisStore
from scripts.swarm_v2 import quota
from scripts.swarm_v2.kubernetes import approved
from scripts.swarm_v2.kubernetes.client import ApiRefused

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

SLUG = "accounts-fixture"
NOW_MS = 1_800_000_000_000
HOUR = 3600


class Secrets:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def read(self, kind, name):
        self.asked.append((kind, name))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_the_approved_accounts_are_the_key_names_of_the_shared_secret():
    api = Secrets(
        {"metadata": {"name": "swarm-claude-accounts"}, "data": {"bkey": "YXBwcm92ZWQ=", "akey": "YXBwcm92ZWQ="}}
    )
    keys = approved.SecretKeys(api)()
    assert keys == frozenset({"akey", "bkey"})
    assert api.asked == [("secrets", "swarm-claude-accounts")]


def test_a_secret_key_that_no_pod_could_carry_is_never_approved():
    data = dict.fromkeys(["okkey", "Upper", "under_score", "dot.ted", "-lead", "a" * 64, "a" * 63], "YXBwcm92ZWQ=")
    assert approved.SecretKeys(Secrets({"data": data}))() == frozenset({"okkey", "a" * 63})


@pytest.mark.parametrize("answer", [None, {"metadata": {}}, {"data": None}])
def test_a_missing_or_empty_shared_secret_approves_no_account(answer):
    assert approved.SecretKeys(Secrets(answer))() == frozenset()


@pytest.mark.parametrize(
    ("error", "name"), [(ApiRefused(403, "Forbidden"), "ApiRefused"), (ConnectionError("down"), "ConnectionError")]
)
def test_an_unreadable_shared_secret_approves_no_account_and_says_why(error, name, capsys):
    assert approved.SecretKeys(Secrets(error))() == frozenset()
    assert (
        capsys.readouterr().out
        == f"controller: Secret swarm-claude-accounts is unreadable ({name}); no account is approved\n"
    )


def test_an_unreadable_shared_secret_line_is_flushed(monkeypatch):
    flushed = []
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: flushed.append(kwargs.get("flush")))
    approved.SecretKeys(Secrets(TimeoutError()))()
    assert flushed == [True]


def _quota():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    return store, quota.QuotaObservations(store, SLUG, lambda token: None, clock=lambda: NOW_MS)


def _observe(store, account, five_used=10.0, week_used=10.0, week_reset=NOW_MS // 1000 + 48 * HOUR, status="allowed"):
    observation = quota.Observation(
        account, "claude", "exe-" + "0" * 32, 1, NOW_MS, status, five_used, week_used, NOW_MS // 1000 + HOUR, week_reset
    )
    store.redis.hset(quota.latest_key("claude"), account, quota.encode(observation))


def _route(keys, held=None):
    store, observations = _quota()
    route = approved.AccountRoute(lambda: frozenset(keys), observations, lambda: dict(held or {}), "claude")
    return store, route


def test_the_launch_account_is_the_approved_key_holding_the_fewest_slots():
    _, route = _route({"akey", "bkey", "ckey"}, {"akey": 1, "ckey": 1})
    assert route.choose(2) == "bkey"


def test_a_key_whose_newest_quota_reading_is_exhausted_is_never_chosen():
    store, route = _route({"akey", "bkey"}, {"bkey": 1})
    _observe(store, "akey", five_used=97.0)
    assert route.choose(2) == "bkey"
    _observe(store, "akey", status="rejected")
    assert route.choose(2) == "bkey"


def test_a_key_whose_fleet_slots_are_full_is_never_chosen():
    _, route = _route({"akey", "bkey"}, {"akey": 2, "bkey": 1})
    assert route.choose(2) == "bkey"
    _, route = _route({"akey", "bkey"}, {"akey": 2, "bkey": 2})
    assert route.choose(2) is None


def test_the_quota_band_lowers_the_launch_cap_of_an_observed_key():
    store, route = _route({"akey", "bkey"}, {"akey": 2, "bkey": 2})
    _observe(store, "akey", five_used=93.0)
    assert route.choose(3) == "bkey"
    _observe(store, "akey", five_used=10.0)
    assert route.choose(3) == "akey"


def test_among_equally_held_keys_the_week_that_resets_soonest_is_spent_first():
    store, route = _route({"akey", "bkey", "ckey"})
    _observe(store, "bkey", week_reset=NOW_MS // 1000 + 72 * HOUR)
    _observe(store, "ckey", week_reset=NOW_MS // 1000 + 24 * HOUR)
    assert route.choose(2) == "ckey"
    _observe(store, "ckey", week_reset=NOW_MS // 1000 + 96 * HOUR)
    assert route.choose(2) == "bkey"


def test_equally_held_unobserved_keys_are_taken_in_name_order():
    _, route = _route({"bkey", "akey"})
    assert route.choose(1) == "akey"


def test_each_approved_key_is_offered_as_a_claude_seat_with_its_cap_held_slots_and_week_reset():
    store, route = _route({"bkey", "akey"}, {"akey": 1})
    _observe(store, "bkey", five_used=60.0, week_reset=NOW_MS // 1000 + 24 * HOUR)
    assert route.seats(5) == [
        session_bands.Seat("claude", "akey", 5, 1),
        session_bands.Seat("claude", "bkey", 4, 0, NOW_MS / 1000 + 24 * HOUR),
    ]


def test_a_key_at_the_last_five_hour_band_waits_behind_a_key_whose_week_resets_later():
    store, route = _route({"akey", "bkey"})
    _observe(store, "akey", five_used=95.0, week_reset=NOW_MS // 1000 + 24 * HOUR)
    _observe(store, "bkey", week_reset=NOW_MS // 1000 + 72 * HOUR)
    assert route.choose(2) == "bkey"


def test_no_approved_key_chooses_no_account():
    _, route = _route(set())
    assert route.choose(2) is None
