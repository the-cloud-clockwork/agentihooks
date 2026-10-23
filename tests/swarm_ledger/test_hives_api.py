import io
import json
import re
from types import SimpleNamespace

import pytest

SLUG = "hives-2026-10-18"
PRINCIPALS = {"admin-token": "", "agent-token": "engineer@1"}
NOW_MS = 1_800_000_000_000


class Headers(dict):
    def get_content_type(self):
        return self.get("Content-Type", "text/plain")


@pytest.fixture
def store(monkeypatch):
    import fakeredis

    from scripts.hive import registry
    from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig
    from scripts.swarm_ledger.api import hives

    monkeypatch.setattr(hives, "time", SimpleNamespace(time=lambda: NOW_MS / 1000))
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "agentihooks", 1, 1))
    for hive_id, ui, beat in (("desk", "yes", NOW_MS - 1000), ("pod", "no", NOW_MS - registry.LIVE_MS - 1)):
        record = registry._default(hive_id)
        record.update(
            ui=ui,
            excluded=[] if ui == "yes" else ["master"],
            max_agents=4,
            heartbeat_at=beat,
            slots=[{"harness": "claude", "account": "luna", "kind": "subscription", "cap": 6}],
        )
        store.redis.hset(registry.key(hive_id), mapping={k: registry._encode(k, v) for k, v in record.items()})
        store.redis.sadd(registry.INDEX, hive_id)
    store.redis.set(f"{registry.key('desk')}:sessions", json.dumps({"claude:luna": 2}))
    record = AgentRecord(
        store.next_name("sw", MASTER),
        MASTER,
        MASTER,
        harness="claude",
        seat="master@sw",
        hive="desk",
        runtime_target={"host_id": "desk"},
    )
    store.start_execution("sw", record)
    return store


def fake_server(store, calls=None):
    from tests.swarm_ledger.api_fakes import as_services

    return as_services(server_parts(store, calls))


def server_parts(store, calls=None):
    calls = [] if calls is None else calls

    def principal(admin, slug, token, agent):
        calls.append((admin, slug, token, agent))
        return PRINCIPALS.get(token)

    return SimpleNamespace(
        ALLOWED_HOSTS={"h"},
        ALLOWED_ORIGINS=set(),
        MAX_BODY=10_000,
        core=SimpleNamespace(SLUG_RE=re.compile(r"[a-z0-9-]+"), loads=json.loads),
        authority=SimpleNamespace(
            caller=lambda headers, admin, slug: principal(
                admin, slug, headers.get("X-Ledger-Token"), headers.get("X-Ledger-Agent")
            )
        ),
        repository=SimpleNamespace(token=lambda slug: "admin-token"),
        swarm_store=lambda: store,
    )


def request(method, path="/api/v1/hives", payload=None, token="admin-token", slug=SLUG):
    raw = b"" if payload is None else json.dumps(payload).encode()
    headers = Headers({"Host": "h", "Content-Type": "application/json", "Content-Length": str(len(raw))})
    if token is not None:
        headers["X-Ledger-Token"] = token
    if slug is not None:
        headers["X-Ledger-Slug"] = slug
    return SimpleNamespace(
        headers=headers, path=path, command=method, rfile=io.BytesIO(raw), exists=lambda name: name == SLUG
    )


def dispatch(handler, store):
    from scripts.swarm_ledger.api import routes

    return routes.dispatch(handler, fake_server(store))


def refusal(handler, store):
    from scripts.swarm_ledger.api.errors import APIError

    with pytest.raises(APIError) as caught:
        dispatch(handler, store)
    return caught.value.status, caught.value.code, str(caught.value)


def test_a_read_lists_every_hive_with_liveness_slots_agents_and_its_master(store):
    from scripts.swarm_ledger.api import resources

    reply = dispatch(request("GET", token=None, slug=None), store)
    desk, pod = reply["data"]
    assert (desk["id"], desk["live"], desk["ui"], desk["excluded"], desk["max_agents"]) == (
        "desk",
        True,
        "yes",
        [],
        4,
    )
    assert desk["slots"] == [{"harness": "claude", "account": "luna", "kind": "subscription", "cap": 6, "agents": 2}]
    assert desk["master"] == {"slug": "sw", "name": store.agents("sw")[0].name}
    assert (pod["id"], pod["live"], pod["master"], pod["slots"][0]["agents"]) == ("pod", False, None, 0)
    assert reply["revision"] == resources.revision(reply["data"])


def test_a_hive_that_never_beat_or_went_quiet_past_a_day_is_not_listed_and_keeps_its_record(store):
    from scripts.hive import registry
    from scripts.swarm_ledger.api import hives

    for hive_id, beat in (("never", 0), ("gone", NOW_MS - hives.GONE_MS - 1), ("edge", NOW_MS - hives.GONE_MS)):
        record = {**registry._default(hive_id), "heartbeat_at": beat}
        store.redis.hset(registry.key(hive_id), mapping={k: registry._encode(k, v) for k, v in record.items()})
        store.redis.sadd(registry.INDEX, hive_id)
    reply = dispatch(request("GET", token=None, slug=None), store)
    assert [hive["id"] for hive in reply["data"]] == ["desk", "edge", "pod"]
    assert refusal(request("GET", "/api/v1/hives/never"), store) == (404, "resource_missing", "No such hive")
    assert {"never", "gone"} <= set(store.redis.smembers(registry.INDEX))
    assert registry.show(store.redis, "gone")["heartbeat_at"] == NOW_MS - hives.GONE_MS - 1


def test_a_hive_named_by_its_id_shows_its_member_name_and_a_set_name_wins(store):
    from scripts.hive import auth, registry

    store.redis.hset(f"{auth.PREFIX}:member:desk", mapping={"name": "workstation"})
    store.redis.hset(f"{auth.PREFIX}:member:pod", mapping={"name": "anton pod"})
    store.redis.hset(registry.key("pod"), "name", "Pod one")
    reply = dispatch(request("GET", token=None, slug=None), store)
    assert [(hive["id"], hive["name"]) for hive in reply["data"]] == [("desk", "workstation"), ("pod", "Pod one")]
    store.redis.delete(f"{auth.PREFIX}:member:desk")
    assert dispatch(request("GET", token=None, slug=None), store)["data"][0]["name"] == "desk"


def test_a_hive_that_beat_at_this_moment_is_listed_as_live(store):
    from scripts.hive import registry

    store.redis.hset(registry.key("desk"), "heartbeat_at", registry._encode("heartbeat_at", NOW_MS))
    reply = dispatch(request("GET", token=None, slug=None), store)
    assert [(hive["id"], hive["live"]) for hive in reply["data"]] == [("desk", True), ("pod", False)]


def test_a_quiet_hive_inside_the_day_is_listed_and_readable_by_id(store):
    from scripts.hive import registry
    from scripts.swarm_ledger.api import hives

    beat = NOW_MS - hives.GONE_MS + 1
    store.redis.hset(registry.key("pod"), "heartbeat_at", registry._encode("heartbeat_at", beat))
    found = dispatch(request("GET", "/api/v1/hives/pod", token=None, slug=None), store)["data"]
    assert (found["id"], found["live"], found["heartbeat_at"]) == ("pod", False, beat)


def test_a_hive_gone_past_a_day_is_not_found_by_id(store):
    from scripts.hive import registry
    from scripts.swarm_ledger.api import hives

    store.redis.hset(registry.key("pod"), "heartbeat_at", registry._encode("heartbeat_at", NOW_MS - hives.GONE_MS - 1))
    assert refusal(request("GET", "/api/v1/hives/pod"), store) == (404, "resource_missing", "No such hive")
    assert registry.show(store.redis, "pod") is not None


def test_a_read_of_one_hive_answers_that_hive_and_a_missing_one_is_not_found(store):
    assert dispatch(request("GET", "/api/v1/hives/pod", token=None, slug=None), store)["data"]["id"] == "pod"
    assert refusal(request("GET", "/api/v1/hives/gone"), store) == (404, "resource_missing", "No such hive")
    assert refusal(request("GET", "/api/v1/hives/pod/extra"), store) == (404, "resource_missing", "No such resource")


def test_a_finished_master_or_one_without_a_hive_is_not_shown(store):
    from dataclasses import replace

    from scripts.swarm.store import MASTER, AgentRecord, SwarmConfig
    from scripts.swarm_ledger.api import hives

    store.create(SwarmConfig("local", "agentihooks", 1, 1))
    store.put_agent("local", AgentRecord("master@local-1", MASTER, MASTER, seat="master@local"))
    assert hives.masters(store) == {"desk": {"slug": "sw", "name": store.agents("sw")[0].name}}
    master = store.agents("sw")[0]
    store.put_agent("sw", replace(master, state="finished"))
    assert hives.masters(store) == {}


def test_the_operator_writes_settings_through_the_hive_registry(store):
    from scripts.hive import registry

    body = {"name": "Desk", "excluded": ["qa"], "prefer": {"eng": 2, "ci": 1}, "max_agents": 6}
    reply = dispatch(request("PATCH", "/api/v1/hives/desk", body), store)
    stored = registry.show(store.redis, "desk")
    assert (stored["name"], stored["excluded"], stored["prefer"], stored["max_agents"]) == (
        "Desk",
        ["qa"],
        {"eng": 2, "ci": 1},
        6,
    )
    assert reply["data"]["name"] == "Desk"
    assert reply["data"]["master"] == {"slug": "sw", "name": store.agents("sw")[0].name}


def test_flags_reach_the_registry_as_yes_or_no(store):
    from scripts.hive import registry

    dispatch(request("PATCH", "/api/v1/hives/pod", {"ephemeral": "yes"}), store)
    assert registry.show(store.redis, "pod")["ephemeral"] == "yes"


def test_settings_become_registry_pairs():
    from scripts.swarm_ledger.api import hives

    assert hives.pairs({"excluded": ["eng", "ci"], "prefer": {"eng": 1}, "max_agents": 3, "ui": "no"}) == [
        "excluded=eng,ci",
        "prefer=eng:1",
        "max-agents=3",
        "ui=no",
    ]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"prefer": {"master": 1}}, "prefer names master, a role this hive excludes"),
        ({"max_agents": 0}, "max-agents must be a positive whole number, not '0'"),
        ({"excluded": ["bad name"]}, "excluded takes lane roles or profiles, not 'bad name'"),
    ],
)
def test_the_registry_refuses_settings_it_does_not_accept(store, body, message):
    from scripts.hive import registry

    before = registry.show(store.redis, "pod")
    assert refusal(request("PATCH", "/api/v1/hives/pod", body), store) == (400, "schema_invalid", message)
    assert registry.show(store.redis, "pod") == before


@pytest.mark.parametrize(
    "body", [{}, {"color": "red"}, {"ui": "maybe"}, {"roles": ["eng"]}, {"excluded": [""]}, {"max_agents": "3"}]
)
def test_the_schema_refuses_bodies_outside_the_settings(store, body):
    expected = (400, "schema_invalid", "Request does not match the resource schema")
    assert refusal(request("PATCH", "/api/v1/hives/pod", body), store) == expected


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [("GET", "/api/v1/hives", None), ("GET", "/api/v1/hives/desk", None), ("PATCH", "/api/v1/hives/pod", {"ui": "no"})],
)
def test_an_unreadable_hive_record_answers_storage_error(store, method, path, payload):
    from scripts.hive import registry

    store.redis.hset(registry.key("pod"), "max_agents", "many")
    expected = (503, "storage_error", "hive pod holds an unreadable max_agents: 'many'")
    assert refusal(request(method, path, payload), store) == expected


@pytest.mark.parametrize(
    ("token", "slug", "message"),
    [
        ("agent-token", SLUG, "Hive settings need the operator"),
        ("wrong", SLUG, "Missing or wrong ledger credential"),
        (None, SLUG, "Missing or wrong ledger credential"),
        ("admin-token", None, "Missing or wrong ledger credential"),
        ("admin-token", "other-ledger", "Missing or wrong ledger credential"),
    ],
)
def test_only_the_operator_writes_hive_settings(store, token, slug, message):
    from scripts.hive import registry

    before = registry.show(store.redis, "pod")
    assert refusal(request("PATCH", "/api/v1/hives/pod", {"max_agents": 9}, token, slug), store) == (
        403,
        "forbidden",
        message,
    )
    assert registry.show(store.redis, "pod") == before


def test_a_write_to_an_unknown_hive_creates_nothing(store):
    from scripts.hive import registry

    assert refusal(request("PATCH", "/api/v1/hives/gone", {"max_agents": 2}), store) == (
        404,
        "resource_missing",
        "No such hive",
    )
    assert registry.show(store.redis, "gone") is None


@pytest.mark.parametrize(("method", "path"), [("PATCH", "/api/v1/hives"), ("DELETE", "/api/v1/hives/pod")])
def test_other_methods_are_refused(store, method, path):
    assert refusal(request(method, path, {"ui": "no"}), store) == (
        405,
        "method_not_allowed",
        "Use GET, or PATCH on one hive",
    )


def test_an_unreachable_swarm_store_answers_storage_error():
    from scripts.swarm.store import Unreachable
    from scripts.swarm_ledger.api import routes
    from scripts.swarm_ledger.api.errors import APIError

    def down():
        raise Unreachable("Redis is unreachable")

    from tests.swarm_ledger.api_fakes import as_services

    server = server_parts(None)
    server.swarm_store = down
    with pytest.raises(APIError) as caught:
        routes.dispatch(request("GET"), as_services(server))
    assert (caught.value.status, caught.value.code, str(caught.value)) == (503, "storage_error", "Redis is unreachable")


@pytest.mark.parametrize(
    ("method", "path", "payload"), [("GET", "/api/v1/hives", None), ("PATCH", "/api/v1/hives/pod", {"ui": "no"})]
)
def test_a_redis_drop_after_the_store_opens_answers_storage_error(store, monkeypatch, method, path, payload):
    import redis

    def drop(*args, **kwargs):
        raise redis.ConnectionError("Connection reset by peer")

    monkeypatch.setattr(store.redis, "hgetall", drop)
    assert refusal(request(method, path, payload), store) == (503, "storage_error", "The swarm store is unreachable")


@pytest.mark.parametrize("hive", ["desk", "pod-2.a_b"])
def test_master_move_runs_the_swarm_master_move_command(hive):
    from scripts.swarm_ledger.ledger_server import control_argv

    assert control_argv({"action": "master_move", "hive": hive}) == ["master", "move", "--to", hive]


@pytest.mark.parametrize("body", [{}, {"hive": ""}, {"hive": "-x"}, {"hive": "a b"}, {"hive": 3}])
def test_master_move_needs_a_hive_id(body):
    from scripts.swarm_ledger.ledger_server import control_argv

    with pytest.raises(ValueError) as caught:
        control_argv({"action": "master_move", **body})
    assert str(caught.value) == "A master move needs a hive id"


def test_master_move_is_an_accepted_operator_control():
    from scripts.swarm_ledger.api import admin, schemas

    schemas.validate(admin.CONTROL, {"action": "master_move", "hive": "desk"})
