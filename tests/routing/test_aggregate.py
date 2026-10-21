import json

import pytest

from scripts.hive import registry
from scripts.routing import aggregate


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


def report(redis, hive, counts, observed, used=20):
    registry.update(redis, hive, ["max-agents=8"])
    redis.hset(
        registry.key(hive),
        mapping={
            "heartbeat_at": 100000,
            "slots": json.dumps(
                [
                    {"harness": "claude", "account": "one", "kind": "subscription", "cap": 6},
                    {"harness": "codex", "account": "one", "kind": "subscription", "cap": 6},
                ]
            ),
        },
    )
    redis.set(f"{registry.key(hive)}:sessions", json.dumps(counts), ex=45)
    redis.set(
        f"{registry.key(hive)}:quota",
        json.dumps(
            {
                "claude:one": {
                    "five_hour": {"used": used, "resets_at": 200},
                    "seven_day": {"used": 30, "resets_at": 300},
                    "observed_at": observed,
                },
            }
        ),
        ex=45,
    )


def test_aggregate_sums_hives_and_selects_freshest_slot_quota(redis):
    report(redis, "a", {"claude:one": 2, "codex:one": 1}, 90)
    report(redis, "b", {"claude:one": 3, "codex:one": 2}, 95, used=40)
    rows = aggregate.read(redis, 100)
    assert rows["claude:one"]["sessions"] == 5
    assert rows["codex:one"]["sessions"] == 3
    assert rows["claude:one"]["quota"] == {
        "five_hour": {"used": 40, "resets_at": 200},
        "seven_day": {"used": 30, "resets_at": 300},
        "observed_at": 95,
    }
    assert rows["codex:one"]["quota"] is None


def test_nonlocal_consumers_use_aggregate_without_credential_probes(redis, monkeypatch):
    from hooks.context import account_sessions
    from scripts.swarm import capacity

    report(redis, "a", {"claude:one": 2, "codex:one": 1}, 90)
    report(redis, "b", {"claude:one": 1, "codex:one": 3}, 95)
    monkeypatch.setenv("AGENTIHOOKS_RUNTIME_BACKEND", "kubernetes")
    monkeypatch.setattr("hooks._redis.get_redis", lambda: redis)
    monkeypatch.setattr("time.time", lambda: 100)

    def forbidden(*args, **kwargs):
        raise AssertionError("controller must use hive quota reports")

    monkeypatch.setattr(capacity.balancer.ClaudeTokenSource, "results", forbidden)
    monkeypatch.setattr(capacity.codex_router.CodexAccountSource, "readings", forbidden)
    assert account_sessions.sessions_by_account() == {"one": 3}
    assert account_sessions.codex_sessions_by_account() == {"one": 4}
    rows = capacity.accounts({"AGENTIHOOKS_RUNTIME_BACKEND": "kubernetes"}, 100)
    claude = next(row for row in rows if row.harness == "claude")
    assert (claude.sessions, claude.five_left, claude.week_left, claude.cap) == (3, 80, 70, 6)


def test_local_backend_counts_other_hives_and_unreported_launches_like_the_reservation(redis, monkeypatch):
    import time

    from hooks.context import account_sessions
    from scripts import claude_quota_balancer as balancer
    from scripts.routing.reservation import LaunchSlots
    from scripts.swarm import capacity

    now = int(time.time() * 1000)
    for hive, counts, heartbeat in (
        ("own", {"claude:one": 1, "_executions": {"claude:one": ["mine"], "codex:three": ["done"]}}, now),
        ("far", {"claude:one": 5, "interactive:claude:one": 1, "codex:one": 2}, now),
        ("near", {"claude:one": 3}, now),
        ("silent", None, now),
        ("gone", {"claude:one": 9}, now - registry.LIVE_MS - 1),
    ):
        registry.update(redis, hive, ["max-agents=8"])
        redis.hset(registry.key(hive), "heartbeat_at", heartbeat)
        if counts is not None:
            redis.set(f"{registry.key(hive)}:sessions", json.dumps(counts))
    slots = LaunchSlots(redis)
    assert slots.reserve("claude:one", "mine", 20, 1000)
    assert slots.reserve("claude:one", "starting", 20, 1000)
    assert slots.reserve("codex:two:b", "spawning", 9, 1000)
    assert slots.reserve("codex:three", "done", 9, 1000)
    assert slots.release("done")
    monkeypatch.delenv("AGENTIHOOKS_RUNTIME_BACKEND", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_HIVE_ID", raising=False)
    monkeypatch.setenv("SWARM_HIVE_ID", "own")
    monkeypatch.setattr("hooks._redis.get_redis", lambda: redis)
    monkeypatch.setattr(account_sessions, "handed_off_pids", set)
    monkeypatch.setattr(account_sessions, "live_sessions", lambda proc, skip, environ=None: {11: "one"})
    monkeypatch.setattr(account_sessions, "_codex_processes", lambda proc: ({}, {}))
    monkeypatch.setattr(account_sessions, "managed_held", set)

    assert account_sessions.sessions_by_account() == {"one": 10}
    assert account_sessions.sessions_by_account(local=True) == {"one": 1}
    assert account_sessions.codex_sessions_by_account() == {"one": 2, "two:b": 1, "three": 0}
    assert not slots.reserve("claude:one", "next", 10, 1000)
    assert not slots.reserve("codex:one", "codex-next", 2, 1000)
    assert slots.reserve("codex:one", "codex-next", 3, 1000)

    window = balancer.QuotaWindow(used=10)
    result = balancer.ProbeResult("one", "allowed", "NORMAL", 90, window, window)
    monkeypatch.setattr(balancer, "cached_observations", lambda **kw: [(time.time(), result)])
    monkeypatch.setattr(balancer, "collect_results", lambda *args, **kwargs: ([], "cached"))
    (row,) = capacity._claude({"AH_CC_TOKEN_one": "fake"}, time.time())
    assert row.sessions == 10
    assert slots.reserve("claude:one", "next", 11, 1000)


def test_aggregate_includes_reservations_once_and_exit_releases(redis, monkeypatch):
    from scripts.routing import reservation

    monkeypatch.setattr("time.time", lambda: 100)
    report(redis, "a", {"claude:one": 0}, 95)
    slots = reservation.LaunchSlots(redis)
    assert slots.reserve("claude:one", "first", 1, 1000)
    assert aggregate.read(redis, 100)["claude:one"]["sessions"] == 1
    assert slots.acknowledge("first")
    redis.set(
        f"{registry.key('a')}:sessions",
        json.dumps(
            {
                "claude:one": 1,
                "_executions": {"claude:one": ["first"]},
            }
        ),
        ex=45,
    )
    assert aggregate.read(redis, 100)["claude:one"]["sessions"] == 1
    assert not slots.reserve("claude:one", "second", 1, 1000)
    before = redis.get(f"{registry.key('a')}:sessions")
    assert slots.release("first")
    assert redis.get(f"{registry.key('a')}:sessions") == before
    assert aggregate.read(redis, 100)["claude:one"]["sessions"] == 0
    assert slots.reserve("claude:one", "second", 1, 1000)


def test_local_process_snapshot_preserves_harness_and_account(tmp_path, monkeypatch):
    from hooks.context import account_sessions
    from tests.context.test_account_sessions import _proc

    root = tmp_path / "proc"
    _proc(root, 1000, "claude", 1, ["claude"], {f"{account_sessions.TOKEN_PREFIX}one": "fixture"})
    _proc(root, 2000, "codex", 1, ["codex", "exec", "prompt"], {f"{account_sessions.CODEX_TOKEN_PREFIX}two": "fixture"})
    assert account_sessions.local_processes(root) == {"claude": {1000: "one"}, "codex": {2000: "two"}}
    assert account_sessions.local_processes(tmp_path / "gone") == {"claude": {}, "codex": {}}
    monkeypatch.setattr(account_sessions, "handed_off_pids", lambda: {1000})
    assert account_sessions.local_processes(root) == {"claude": {}, "codex": {2000: "two"}}


def test_aggregate_keeps_newer_quota_and_sums_interactive_masters(redis):
    report(redis, "a", {"claude:one": 1, "interactive:claude:one": 2}, 95, used=40)
    report(redis, "b", {"interactive:claude:one": 3}, 90)
    rows = aggregate.read(redis, 100)
    assert rows["claude:one"]["sessions"] == 1
    assert rows["claude:one"]["masters"] == 5
    assert rows["claude:one"]["quota"]["observed_at"] == 95
    assert rows["claude:one"]["quota"]["five_hour"]["used"] == 40
    assert rows["codex:one"]["sessions"] == 0
    assert rows["codex:one"]["masters"] == 0


def test_aggregate_excludes_stale_reports_and_handles_missing_quota(redis):
    report(redis, "a", {"claude:one": 1}, 90)
    redis.delete(f"{registry.key('a')}:quota")
    rows = aggregate.read(redis, 100)
    assert rows["claude:one"]["quota"] is None
    assert aggregate.read(redis, 191) == {}
    redis.delete(f"{registry.key('a')}:sessions")
    assert aggregate.read(redis, 100) == {}


@pytest.mark.parametrize("account", ["one", "tenant:one"])
def test_aggregate_retains_the_complete_unreported_reservation_slot(redis, account):
    from scripts.routing import reservation

    assert reservation.LaunchSlots(redis).reserve(f"codex:{account}", "pending", 1, 1000)
    assert aggregate.read(redis, 100) == {
        f"codex:{account}": {
            "harness": "codex",
            "account": account,
            "kind": "subscription",
            "quota": None,
            "sessions": 1,
        }
    }


def test_global_capacity_preserves_api_policy_and_future_reset_fields(redis, monkeypatch):
    from scripts.routing import reservation
    from scripts.swarm import capacity

    environ = {"AGENTIHOOKS_RUNTIME_BACKEND": "kubernetes"}
    report(redis, "a", {"claude:one": 2, "codex:one": 1}, 90)
    redis.hset(
        registry.key("a"),
        "slots",
        json.dumps(
            [
                {"harness": "claude", "account": "one", "kind": "subscription", "cap": 6},
                {"harness": "codex", "account": "one", "kind": "api", "cap": 4},
            ]
        ),
    )
    quota_key = f"{registry.key('a')}:quota"
    quota = json.loads(redis.get(quota_key))
    quota["claude:one"]["seven_day"]["used"] = 70
    redis.set(quota_key, json.dumps(quota))
    monkeypatch.setattr("hooks._redis.get_redis", lambda: redis)
    monkeypatch.setattr("time.time", lambda: 100)
    monkeypatch.setattr(capacity.place, "_client", lambda env: redis if env == environ else None)
    monkeypatch.setattr(
        capacity.place,
        "open_store",
        lambda client, env: (
            {"codex-api-max-sessions": 4, "codex-api-weight": 3} if client is redis and env == environ else {}
        ),
    )
    monkeypatch.setattr(
        reservation,
        "current",
        lambda harness, env: reservation.Reservation("claude", "one", 1) if harness == "claude" else None,
    )
    rows = capacity.accounts(environ, 100)
    assert rows == [
        capacity.Account(
            "claude",
            "one",
            "CLOSED",
            2,
            80,
            30,
            0,
            300,
            five_resets_at=200,
            reserved="reserved for 1 live master: workers stop at 70% of the week",
            unreserved_cap=6,
        ),
        capacity.Account("codex", "one", "OPEN", 1, None, None, 4, kind="api", weight=3),
    ]
    redis.hset(
        registry.key("a"),
        "slots",
        json.dumps([{"harness": "claude", "account": "one", "kind": "subscription", "cap": 6}]),
    )
    redis.set(
        f"{registry.key('a')}:quota",
        json.dumps(
            {
                "claude:one": {
                    "five_hour": {"used": 100, "resets_at": 99},
                    "seven_day": {"used": 100, "resets_at": 99},
                    "observed_at": 90,
                }
            }
        ),
    )
    monkeypatch.setattr(reservation, "current", lambda harness, env: None)
    assert capacity.accounts(environ, 100) == [capacity.Account("claude", "one", "OPEN", 2, 100, 100, 6)]


@pytest.mark.parametrize("missing", ["stale", "sessions"])
def test_aggregate_keeps_reading_after_an_unavailable_hive(redis, missing):
    report(redis, "a", {"claude:one": 99}, 90)
    report(redis, "b", {"claude:one": 2}, 95)
    if missing == "stale":
        redis.hset(registry.key("a"), "heartbeat_at", 1)
    else:
        redis.delete(f"{registry.key('a')}:sessions")
    assert aggregate.read(redis, 100)["claude:one"]["sessions"] == 2


def test_aggregate_uses_milliseconds_at_the_live_hive_boundary(redis):
    report(redis, "a", {"claude:one": 1}, 90)
    assert aggregate.read(redis, 190)["claude:one"]["sessions"] == 1


@pytest.mark.parametrize(("first", "second", "cap"), [(95, 90, 6), (90, 95, 4), (95, 95, 6)])
def test_aggregate_selects_slot_metadata_and_keeps_the_first_equal_observation(redis, first, second, cap):
    report(redis, "a", {"claude:one": 1}, first, used=20)
    report(redis, "b", {"claude:one": 2}, second, used=40)
    redis.hset(
        registry.key("b"), "slots", json.dumps([{"harness": "claude", "account": "one", "kind": "api", "cap": 4}])
    )
    row = aggregate.read(redis, 100)["claude:one"]
    assert row["cap"] == cap
    assert row["kind"] == ("subscription" if cap == 6 else "api")
    assert row["quota"]["five_hour"]["used"] == (20 if cap == 6 else 40)
