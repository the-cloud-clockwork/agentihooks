import json
from collections.abc import Callable, Collection, Mapping
from typing import Any

import pytest

from hooks.context import account_sessions
from scripts import session_bands
from scripts.hive import launch, registry
from scripts.hive.placement import account_key
from scripts.routing import aggregate
from scripts.swarm import capacity
from scripts.swarm.runtime import HerdrRuntime
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm_v2 import quota
from scripts.swarm_v2.kubernetes import approved
from scripts.swarm_v2.kubernetes.client import ApiRefused

pytestmark = pytest.mark.unit

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


def test_codex_approval_reads_only_access_key_names_from_its_redacted_index():
    api = Secrets(
        {
            "data": dict.fromkeys(
                [
                    "AH_CX_TOKEN_alpha_personal",
                    "AH_CX_TOKEN_beta_business",
                    "AH_CX_REFRESH_alpha_personal",
                    "unrelated",
                    "AH_CX_TOKEN_",
                    "AH_CX_TOKEN_Upper",
                    "AH_CX_TOKEN_bad.dot",
                    "AH_CX_TOKEN_wrong-dash",
                ],
                "YXBwcm92ZWQ=",
            )
        }
    )
    assert approved.SecretKeys(api, "codex")() == frozenset({"alpha-personal", "beta-business"})
    assert api.asked == [("secrets", "swarm-codex-accounts")]


@pytest.mark.parametrize("answer", [None, {}, {"data": {}}, {"data": {"AH_CX_REFRESH_alpha": "YXBwcm92ZWQ="}}])
def test_a_codex_index_without_access_keys_approves_no_account(answer):
    assert approved.SecretKeys(Secrets(answer), "codex")() == frozenset()


def test_an_unreadable_codex_index_approves_no_account_and_names_its_index(capsys):
    assert approved.SecretKeys(Secrets(TimeoutError()), "codex")() == frozenset()
    assert "Secret swarm-codex-accounts is unreadable" in capsys.readouterr().out


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


def _route(
    keys: Collection[str],
    held: Mapping[str, int] | None = None,
    advertised: Callable[[float], Mapping[str, Mapping[str, Any]]] | None = None,
    harness: str = "claude",
) -> tuple[RedisStore, approved.AccountRoute]:
    store, observations = _quota()
    route = approved.AccountRoute(
        lambda: frozenset(keys), observations, lambda: dict(held or {}), harness, advertised or (lambda now: {})
    )
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


def test_a_week_that_already_reset_is_not_spent_before_one_still_to_come():
    store, route = _route({"akey", "bkey"})
    _observe(store, "akey", week_reset=NOW_MS // 1000 - HOUR)
    _observe(store, "bkey")
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


def test_the_planned_account_is_kept_while_it_has_a_free_slot():
    _, route = _route({"akey", "bkey"}, {"bkey": 1})
    assert route.choose(2, planned="bkey") == "bkey"
    _, route = _route({"akey", "bkey"}, {"bkey": 2})
    assert route.choose(2, planned="bkey") == "akey"
    assert route.choose(2, planned="gone") == "akey"


def test_a_quota_successor_never_lands_on_the_account_handing_off_or_an_unread_one():
    store, route = _route({"akey", "bkey", "ckey"}, {"bkey": 1})
    _observe(store, "akey")
    _observe(store, "bkey")
    assert route.choose(2, predecessor=("claude", "akey")) == "bkey"
    assert route.choose(2, planned="akey", predecessor=("claude", "akey")) == "bkey"
    assert route.choose(2, planned="ckey", predecessor=("claude", "akey")) == "bkey"


def test_a_quota_successor_finds_no_account_when_only_the_predecessor_is_read():
    store, route = _route({"akey", "bkey"})
    _observe(store, "akey")
    assert route.choose(2, predecessor=("claude", "akey")) is None


def test_the_fleet_rows_are_one_claude_row_per_approved_key_with_its_fleet_held_slots():
    _, route = _route({"bkey", "akey"}, {"akey": 1, "elsewhere": 3})
    rows = approved.FleetAccounts(route, 2)({}, NOW_MS / 1000, refresh=True)
    assert rows == [
        capacity.Account("claude", "akey", "OPEN", 1, None, None, 2),
        capacity.Account("claude", "bkey", "OPEN", 0, None, None, 2),
    ]


def test_an_observed_key_offers_its_quota_band_under_the_launch_cap_with_its_windows():
    store, route = _route({"akey"})
    _observe(store, "akey", five_used=93.0, week_used=40.0)
    [row] = approved.FleetAccounts(route, 4)({}, NOW_MS / 1000, refresh=False)
    assert row == capacity.Account(
        "claude", "akey", "OPEN", 0, 7.0, 60.0, 2, NOW_MS // 1000 + 48 * HOUR, five_resets_at=NOW_MS // 1000 + HOUR
    )
    _observe(store, "akey", five_used=10.0)
    assert approved.FleetAccounts(route, 4)({}, NOW_MS / 1000, refresh=False)[0].cap == 4


def test_an_exhausted_key_is_a_closed_row():
    store, route = _route({"akey"})
    _observe(store, "akey", status="rejected")
    [row] = approved.FleetAccounts(route, 2)({}, NOW_MS / 1000, refresh=False)
    assert (row.state, row.cap) == ("CLOSED", 0)


CLOSED_SLOT, OPEN_KEY = "nestorcolt_smatgmail_com", "thecloudclockworkatgmail-com"
WEEK_RESET = NOW_MS // 1000 + 5 * HOUR


def _reading(
    week_used: float | None,
    five_used: float | None = None,
    observed_at: float | None = NOW_MS / 1000,
    five_reset: int | None = None,
    week_reset: int = WEEK_RESET,
) -> dict[str, Any]:
    return {
        "five_hour": {"used": five_used, "resets_at": five_reset},
        "seven_day": {"used": week_used, "resets_at": week_reset},
        "observed_at": observed_at,
    }


def _hives(
    reading: Mapping[str, Any], slot: str = CLOSED_SLOT
) -> tuple[Callable[[float], dict[str, Mapping[str, Any]]], list[float]]:
    seen: list[float] = []

    def advertised(now: float) -> dict[str, Mapping[str, Any]]:
        seen.append(now)
        return {account_key(slot): reading}

    return advertised, seen


def test_a_closed_account_a_hive_advertises_gets_no_placement() -> None:
    advertised, seen = _hives(_reading(97.0))
    _, route = _route({account_key(CLOSED_SLOT), OPEN_KEY}, advertised=advertised)
    rows = approved.FleetAccounts(route, 1)({}, NOW_MS / 1000, refresh=False)
    assert rows == [
        capacity.Account("claude", account_key(CLOSED_SLOT), "CLOSED", 0, None, 3.0, 0, WEEK_RESET),
        capacity.Account("claude", OPEN_KEY, "OPEN", 0, None, None, 1),
    ]
    assert seen == [NOW_MS / 1000]
    assert [row.name for row in launch._open_rows(rows)] == [OPEN_KEY]
    assert route.choose(1, planned=account_key(CLOSED_SLOT)) == OPEN_KEY


def test_a_quota_successor_never_lands_on_a_closed_account_a_hive_advertises() -> None:
    store, route = _route({"akey", account_key(CLOSED_SLOT)}, advertised=_hives(_reading(97.0))[0])
    _observe(store, "akey")
    assert route.choose(2, planned=account_key(CLOSED_SLOT), predecessor=("claude", "akey")) is None


def test_an_open_account_a_hive_advertises_keeps_its_band_under_the_launch_cap_with_its_windows() -> None:
    reading = _reading(30.0, five_used=50.0, five_reset=NOW_MS // 1000 + HOUR)
    _, route = _route({account_key(CLOSED_SLOT)}, advertised=_hives(reading)[0])
    [row] = approved.FleetAccounts(route, 6)({}, NOW_MS / 1000, refresh=False)
    assert row == capacity.Account(
        "claude",
        account_key(CLOSED_SLOT),
        "OPEN",
        0,
        50.0,
        70.0,
        4,
        WEEK_RESET,
        five_resets_at=NOW_MS // 1000 + HOUR,
    )


def test_a_codex_account_a_hive_advertises_is_banded_by_its_week_alone() -> None:
    _, route = _route({"codexkey"}, advertised=_hives(_reading(30.0, five_used=99.0), "codexkey")[0], harness="codex")
    [row] = approved.FleetAccounts(route, 6)({}, NOW_MS / 1000, refresh=False)
    assert (row.harness, row.state, row.cap) == ("codex", "OPEN", 6)


def test_an_advertised_reading_without_a_week_keeps_the_launch_cap() -> None:
    _, route = _route({account_key(CLOSED_SLOT)}, advertised=_hives(_reading(None))[0])
    [row] = approved.FleetAccounts(route, 2)({}, NOW_MS / 1000, refresh=False)
    assert (row.state, row.cap, row.week_left) == ("OPEN", 2, None)


def test_a_fleet_reading_outranks_what_the_hives_advertise() -> None:
    store, route = _route({"akey"}, advertised=_hives(_reading(97.0), "akey")[0])
    _observe(store, "akey")
    [row] = approved.FleetAccounts(route, 2)({}, NOW_MS / 1000, refresh=False)
    assert (row.state, row.cap) == ("OPEN", 2)


def _hive(store: RedisStore, hive_id: str, heartbeat_ms: int, quotas: Mapping[str, Mapping[str, Any]]) -> None:
    record = {**registry._default(hive_id), "heartbeat_at": heartbeat_ms}
    store.redis.hset(
        registry.key(hive_id), mapping={key: registry._encode(key, value) for key, value in record.items()}
    )
    store.redis.sadd(registry.INDEX, hive_id)
    store.redis.set(f"{registry.key(hive_id)}:quota", json.dumps(quotas))


def test_hives_advertise_each_account_by_its_controller_key_with_the_newest_fresh_reading() -> None:
    store, _ = _quota()
    now = NOW_MS / 1000
    stale = now - session_bands.FRESH_SECONDS - 1
    _hive(
        store,
        "one",
        NOW_MS,
        {
            f"claude:{CLOSED_SLOT}": _reading(97.0, observed_at=now - 60),
            f"codex:{CLOSED_SLOT}": _reading(10.0),
            "claude:stale_slot": _reading(50.0, observed_at=stale),
            "claude:unread_slot": _reading(None, observed_at=None),
        },
    )
    _hive(store, "two", NOW_MS, {f"claude:{CLOSED_SLOT}": _reading(40.0, observed_at=now - 120)})
    _hive(store, "three", NOW_MS, {f"claude:{CLOSED_SLOT}": _reading(90.0, observed_at=now - 90)})
    _hive(store, "gone", NOW_MS - registry.LIVE_MS - 1, {"claude:gone_slot": _reading(10.0)})
    assert aggregate.advertised(store.redis, "claude", now) == {
        account_key(CLOSED_SLOT): _reading(97.0, observed_at=now - 60)
    }
    assert aggregate.advertised(store.redis, "codex", now) == {account_key(CLOSED_SLOT): _reading(10.0)}


def test_of_equally_new_hive_readings_the_lowest_hive_id_wins() -> None:
    store, _ = _quota()
    now = NOW_MS / 1000
    for hive_id, week_used in (("hive-c", 10.0), ("hive-a", 97.0), ("hive-b", 40.0)):
        _hive(store, hive_id, NOW_MS, {f"claude:{CLOSED_SLOT}": _reading(week_used)})
    assert aggregate.advertised(store.redis, "claude", now) == {account_key(CLOSED_SLOT): _reading(97.0)}


def _sizing(runtime):
    runtime.host = lambda: None
    config = SwarmConfig(SLUG, "agentihooks", max_eng=2, max_ci=1)
    return runtime.quota_capacity(config, [], NOW_MS / 1000, {"eng": 2, "ci": 1, "plan": 0})


def test_a_runtime_with_a_fleet_source_sizes_its_lanes_without_reading_local_accounts(monkeypatch):
    def local(*args, **kwargs):
        raise AssertionError("the deployed controller read local accounts")

    monkeypatch.setattr(capacity, "accounts", local)
    monkeypatch.setattr(account_sessions, "sessions_by_account", local)
    _, route = _route({"akey", "bkey"}, {"akey": 1})
    herdr = HerdrRuntime()
    herdr.quota_accounts = approved.FleetAccounts(route, 2)
    decision = _sizing(herdr)
    assert decision["effective"] == {"eng": 1, "ci": 1, "plan": 0}
    assert decision["placeable"] == {"claude": 3, "codex": 0}
    assert [(row["name"], row["sessions"]) for row in decision["accounts"]] == [("akey", 1), ("bkey", 0)]


def test_a_runtime_with_a_fleet_source_answers_capacity_before_its_first_sizing_from_fleet_rows(monkeypatch):
    def local(*args, **kwargs):
        raise AssertionError("the deployed controller read local accounts")

    monkeypatch.setattr(capacity, "accounts", local)
    monkeypatch.setattr(account_sessions, "sessions_by_account", local)
    _, route = _route({"akey"}, {"akey": 1})
    herdr = HerdrRuntime(choose=local)
    herdr.quota_accounts = approved.FleetAccounts(route, 2)
    assert herdr.has_capacity(None) is True
    _, full = _route({"akey"}, {"akey": 2})
    herdr = HerdrRuntime(choose=local)
    herdr.quota_accounts = approved.FleetAccounts(full, 2)
    assert herdr.has_capacity(None) is False


def test_a_runtime_with_a_fleet_source_sees_a_slot_a_launch_took_after_its_sizing(monkeypatch):
    def local(*args, **kwargs):
        raise AssertionError("the deployed controller read local accounts")

    monkeypatch.setattr(capacity, "accounts", local)
    held = {"akey": 1}
    _, observations = _quota()
    route = approved.AccountRoute(
        lambda: frozenset({"akey"}), observations, lambda: dict(held), "claude", lambda now: {}
    )
    herdr = HerdrRuntime(choose=local)
    herdr.quota_accounts = approved.FleetAccounts(route, 2)
    _sizing(herdr)
    assert herdr.has_capacity(None) is True
    held["akey"] = 2
    assert herdr.has_capacity(None) is False


def test_a_capacity_check_reads_the_fleet_source_with_the_environment_and_clock_and_no_refresh(monkeypatch):
    def local(*args, **kwargs):
        raise AssertionError("the deployed controller read local accounts")

    calls = []
    monkeypatch.setenv("AGENTIHOOKS_FLEET_PROBE", "seen")
    monkeypatch.setattr("scripts.swarm.runtime.time.time", lambda: 1234.5)
    herdr = HerdrRuntime(choose=local)
    herdr.quota_accounts = lambda environ, now, refresh: calls.append((environ, now, refresh)) or []
    assert herdr.has_capacity(None) is False
    [(environ, now, refresh)] = calls
    assert (environ["AGENTIHOOKS_FLEET_PROBE"], now, refresh) == ("seen", 1234.5, False)


def test_a_workstation_runtime_still_sizes_its_lanes_from_its_local_accounts(monkeypatch):
    asked = []
    row = capacity.Account("claude", "local", "OPEN", 0, 80.0, 80.0, 3)
    monkeypatch.setattr(capacity, "accounts", lambda environ, now, refresh=True: asked.append((now, refresh)) or [row])
    herdr = HerdrRuntime()
    assert herdr.quota_accounts is None
    decision = _sizing(herdr)
    assert asked == [(NOW_MS / 1000, False)]
    assert decision["effective"] == {"eng": 1, "ci": 1, "plan": 0}
    assert decision["placeable"] == {"claude": 3, "codex": 0}
