import base64
import http.client
import json
import threading
from dataclasses import asdict

import pytest

from scripts.swarm.store import SwarmError
from scripts.swarm_v2.auth_context import LaunchAuthority, LaunchKey
from tests.sv2_ldg02_cases import SLUG, World
from tests.test_swarm_v2_broadcasts import CHANNELS, FOLLOWUP, OPERATOR, REMOTE, WARNING

pytestmark = pytest.mark.unit

CLAIM = "/v2/broadcasts/claim"
EXPIRES_MS = 301_000


class Fleet(World):
    def __init__(self, monkeypatch):
        from scripts.swarm_v2.api.broadcasts import BroadcastsAPI
        from scripts.swarm_v2.broadcasts import FleetBroadcasts

        super().__init__(monkeypatch)
        self.broadcasts = BroadcastsAPI(self.grants, self.store, SLUG)
        self.publisher = FleetBroadcasts(self.store, SLUG, lambda token: self.grants.bound(SLUG, token), self.author)
        self.started = 0

    def author(self, token):
        if token != OPERATOR:
            raise SwarmError("unauthenticated")
        return "operator"

    def worker(self, seat=REMOTE):
        self.started += 1
        agent, token = self.start(seat, f"task-{self.started}")
        assert self.register(agent, token)[0] == 200
        return agent, token

    def announce(self, draft=WARNING):
        return self.publisher.publish_operator(OPERATOR, dict(draft))

    def claim(self, token, body):
        return self.broadcasts.route("POST", CLAIM, f"Bearer {token}", body)

    def deliveries(self):
        return sorted(self.store.redis.scan_iter(match="*broadcast-deliveries*"))


@pytest.fixture
def world(monkeypatch):
    return Fleet(monkeypatch)


def detail(error_class, message):
    retry = "same_request" if error_class == "dependency_unavailable" else "new_request"
    return {"error_class": error_class, "operation_id": "unknown", "retry": retry, "message": message}


def claims_of(token):
    return json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))


def test_a_registered_worker_claims_its_fleet_broadcast_through_the_api(world):
    from scripts.swarm_v2.broadcasts import _delivery

    agent, token = world.worker()
    published = world.announce()
    status, answer = world.claim(token, {"channels": CHANNELS})
    assert status == 200
    assert set(answer) == {"deliveries"}
    [found] = answer["deliveries"]
    assert _delivery(found).broadcast == published
    assert asdict(_delivery(found)) == found
    assert world.publisher.delivery(REMOTE, "fleet-warning")["execution_id"] == agent.execution_id


def test_a_claim_id_replays_the_first_answer(world):
    _, token = world.worker()
    world.announce()
    first = world.claim(token, {"channels": CHANNELS, "claim_id": "claim-1"})
    assert first[0] == 200
    assert len(first[1]["deliveries"]) == 1
    world.announce(FOLLOWUP)
    assert world.claim(token, {"channels": CHANNELS, "claim_id": "claim-1"}) == first
    fresh = world.claim(token, {"channels": CHANNELS, "claim_id": "claim-2"})
    assert [d["broadcast"]["broadcast_id"] for d in fresh[1]["deliveries"]] == ["fleet-followup", "fleet-warning"]


def test_a_claim_on_other_channels_receives_nothing(world):
    _, token = world.worker()
    world.announce()
    assert world.claim(token, {"channels": ["brain"]}) == (200, {"deliveries": []})
    assert world.deliveries() == []


def forged(world, token):
    other = LaunchAuthority(world.store, LaunchKey("fixture-key", b"y" * 32), "controller", "workers")
    return other.sign(claims_of(token))


def foreign(world, token):
    return world.grants.sign({**claims_of(token), "swarm_id": "other"})


def tampered(world, token):
    head, _, signature = token.split(".")
    payload = json.dumps({**claims_of(token), "seat_id": "eng-9@fixture"}).encode()
    return f"{head}.{base64.urlsafe_b64encode(payload).decode().rstrip('=')}.{signature}"


@pytest.mark.parametrize(
    ("make", "status", "error_class", "message"),
    [
        (forged, 401, "unauthenticated", "launch grant signature is invalid"),
        (tampered, 401, "unauthenticated", "launch grant signature is invalid"),
        (foreign, 403, "forbidden_scope", "launch grant belongs to another swarm"),
    ],
)
def test_a_forged_or_foreign_grant_claims_nothing(world, make, status, error_class, message):
    _, token = world.worker()
    world.announce()
    assert world.claim(make(world, token), {"channels": CHANNELS}) == (status, detail(error_class, message))
    assert world.deliveries() == []


def test_an_unregistered_grant_claims_nothing(world):
    _, token = world.start(REMOTE, "task-unregistered")
    world.announce()
    assert world.claim(token, {"channels": CHANNELS}) == (
        401,
        detail("unauthenticated", "launch grant is not registered"),
    )
    assert world.deliveries() == []


def test_a_superseded_grant_claims_nothing(world):
    agent, token = world.worker()
    world.start(REMOTE, agent.task, previous=agent.execution_id)
    world.announce()
    assert world.claim(token, {"channels": CHANNELS}) == (
        409,
        detail("stale_generation", "launch grant is for a superseded execution"),
    )
    assert world.deliveries() == []


def test_a_grant_counts_until_the_second_it_expires(world):
    _, token = world.worker()
    world.announce()
    world.clock[0] = EXPIRES_MS - 1000
    assert world.claim(token, {"channels": CHANNELS})[0] == 200
    world.clock[0] = EXPIRES_MS
    assert world.claim(token, {"channels": CHANNELS}) == (401, detail("unauthenticated", "launch grant has expired"))


@pytest.mark.parametrize("prefix", ["", "Bearer", "Token "])
def test_a_claim_without_a_bearer_credential_is_unauthenticated(world, prefix):
    _, token = world.worker()
    world.announce()
    status, refusal = world.broadcasts.route("POST", CLAIM, f"{prefix}{token}", {"channels": CHANNELS})
    assert (status, refusal) == (401, detail("unauthenticated", "a bearer credential is required"))
    assert world.deliveries() == []


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (None, "the request carries channels and an optional claim_id"),
        ({}, "the request carries channels and an optional claim_id"),
        ({"channels": [], "extra": 1}, "the request carries channels and an optional claim_id"),
        ({"channels": "amygdala"}, "channels must be a list of channel names"),
        ({"channels": ["amygdala", 1]}, "channels must be a list of channel names"),
        ({"channels": [], "claim_id": 5}, "claim_id must be a lowercase name"),
        ({"channels": [], "claim_id": "Claim One"}, "claim_id must be a lowercase name"),
    ],
)
def test_a_malformed_claim_is_an_invalid_request(world, body, message):
    _, token = world.worker()
    world.announce()
    assert world.claim(token, body) == (400, detail("invalid_request", message))
    assert world.deliveries() == []


@pytest.mark.parametrize(("method", "path"), [("GET", CLAIM), ("POST", "/v2/broadcasts/acknowledge")])
def test_an_unrouted_method_or_path_is_no_broadcast_endpoint(world, method, path):
    _, token = world.worker()
    assert world.broadcasts.route(method, path, f"Bearer {token}", {"channels": CHANNELS}) == (
        404,
        detail("invalid_request", "no such broadcast endpoint"),
    )


@pytest.mark.parametrize(
    ("raised", "status", "error_class", "message"),
    [
        ("stale_generation", 409, "stale_generation", "a newer attempt of this seat already claimed"),
        (
            "dependency_unavailable",
            503,
            "dependency_unavailable",
            "fleet broadcasts kept changing; nothing was claimed",
        ),
        ("distribution_disabled", 503, "dependency_unavailable", "fleet broadcasts kept changing; nothing was claimed"),
        ("forbidden_scope", 403, "forbidden_scope", "the launch grant is outside this swarm"),
    ],
)
def test_a_fleet_refusal_keeps_its_swarm_api_class(world, monkeypatch, raised, status, error_class, message):
    _, token = world.worker()

    def refuse(*args):
        raise SwarmError(raised)

    monkeypatch.setattr(world.broadcasts.fleet, "claim", refuse)
    assert world.claim(token, {"channels": CHANNELS}) == (status, detail(error_class, message))


def test_an_unreachable_store_is_a_dependency_failure(world, monkeypatch):
    import redis

    _, token = world.worker()

    def down(*args):
        raise redis.ConnectionError("down")

    monkeypatch.setattr(world.grants, "registration", down)
    assert world.claim(token, {"channels": CHANNELS}) == (
        503,
        detail("dependency_unavailable", "the broadcast store is unavailable"),
    )


def test_the_worker_api_never_publishes_as_the_operator(world):
    from scripts.swarm_v2.auth_context import GrantRefused

    with pytest.raises(GrantRefused) as refused:
        world.broadcasts.fleet.publish_operator(OPERATOR, dict(WARNING))
    assert (refused.value.error_class, str(refused.value)) == (
        "forbidden_scope",
        "workers never publish as the operator",
    )


@pytest.fixture
def served(world):
    from scripts.swarm_v2.api.server import Routes, serve

    server = serve(Routes(world.api, None, broadcasts=world.broadcasts), "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def send(port, method, path, token, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    data = b"" if body is None else json.dumps(body).encode()
    connection.request(method, path, body=data, headers={"Authorization": f"Bearer {token}"})
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    return response.status, json.loads(raw)


def test_the_served_api_carries_broadcast_claims_beside_executions(world, served):
    agent, token = world.start(REMOTE, "task-served")
    registration = {"execution_id": agent.execution_id, "generation": 1}
    assert send(served, "POST", "/v2/executions/register", token, registration)[0] == 200
    world.announce()
    status, answer = send(served, "POST", CLAIM, token, {"channels": CHANNELS})
    assert (status, [d["broadcast"]["broadcast_id"] for d in answer["deliveries"]]) == (200, ["fleet-warning"])
    assert send(served, "POST", CLAIM, foreign(world, token), {"channels": CHANNELS}) == (
        403,
        detail("forbidden_scope", "launch grant belongs to another swarm"),
    )


def test_an_empty_bearer_credential_is_unauthenticated(world):
    world.announce()
    assert world.broadcasts.route("POST", CLAIM, "Bearer ", {"channels": CHANNELS}) == (
        401,
        detail("unauthenticated", "a bearer credential is required"),
    )
    assert world.deliveries() == []


def test_only_broadcast_paths_reach_the_broadcast_api(world):
    from scripts.swarm_v2.api.server import Routes

    _, token = world.worker()
    routes = Routes(world.api, None, broadcasts=world.broadcasts)
    assert Routes(world.api, None).route("POST", CLAIM, f"Bearer {token}", {}) == (
        404,
        detail("invalid_request", "no such execution endpoint"),
    )
    assert routes.route("POST", "/v2/broadcastsX", f"Bearer {token}", {}) == (
        404,
        detail("invalid_request", "no such execution endpoint"),
    )
    assert routes.route("POST", "/v2/broadcasts/other", f"Bearer {token}", {}) == (
        404,
        detail("invalid_request", "no such broadcast endpoint"),
    )
