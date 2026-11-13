import json
import threading
import urllib.error
import urllib.request

import pytest

from scripts.ci_lab import lease_api, leases, queue
from scripts.ci_lab.units import Unit


class Signer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def upload_url(self, job: str, name: str) -> str:
        self.calls.append((job, name))
        return f"https://objects.test/{job}/{name}?signed"


class Clock:
    def __init__(self) -> None:
        self.now = 1000
        self.sleeps: list[int] = []

    def __call__(self) -> int:
        return self.now

    def sleep(self, ms: int) -> None:
        self.sleeps.append(ms)
        self.now += ms


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def jobs() -> queue.Queue:
    import fakeredis

    return queue.Queue(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))


@pytest.fixture
def lab(jobs: queue.Queue, clock: Clock) -> leases.Leases:
    return leases.Leases(jobs, ttl_ms=500, clock=clock, sleep=clock.sleep, poll_ms=50)


@pytest.fixture
def signer() -> Signer:
    return Signer()


def post(lab: leases.Leases, signer: Signer, path: str, body: object) -> tuple[int, dict[str, object]]:
    return lease_api.respond(lab, signer, "POST", path, json.dumps(body).encode())


def granted(lab: leases.Leases, signer: Signer, jobs: queue.Queue) -> dict[str, object]:
    jobs.enqueue([Unit(kind="test", key="a", members=("tests/a.py::t",))], "h1", {})
    status, body = post(lab, signer, "/lease", {"worker": "w1", "free_slots": 1, "capabilities": [], "wait_ms": 0})
    assert status == 200
    return body["leases"][0]


def test_lease_returns_the_grant_payload(lab: leases.Leases, signer: Signer, jobs: queue.Queue) -> None:
    grant = granted(lab, signer, jobs)

    token = grant.pop("token")
    assert isinstance(token, str) and len(token) == 32
    assert grant == {
        "job": "h1:test:a",
        "head": "h1",
        "kind": "test",
        "key": "a",
        "members": ["tests/a.py::t"],
        "slots": 1,
        "expires_ms": 1500,
    }


def test_lease_passes_capabilities_and_caps_the_wait(
    lab: leases.Leases, signer: Signer, jobs: queue.Queue, clock: Clock
) -> None:
    jobs.enqueue(
        [Unit(kind="test", key="web", members=())],
        "h1",
        {"web": queue.Requirement(capabilities=frozenset({"browser"}))},
    )

    assert post(
        lab, signer, "/lease", {"worker": "w1", "free_slots": 1, "capabilities": ["net"], "wait_ms": 10**9}
    ) == (
        200,
        {"leases": []},
    )
    assert clock.now == 1000 + lease_api.MAX_WAIT_MS
    status, body = post(
        lab, signer, "/lease", {"worker": "w1", "free_slots": 1, "capabilities": ["browser"], "wait_ms": 0}
    )
    assert status == 200
    assert [grant["job"] for grant in body["leases"]] == ["h1:test:web"]


def test_heartbeat_and_complete_need_the_token(
    lab: leases.Leases, signer: Signer, jobs: queue.Queue, clock: Clock
) -> None:
    grant = granted(lab, signer, jobs)
    clock.now = 1200

    assert post(lab, signer, "/heartbeat", {"job": grant["job"], "token": "wrong"}) == (
        403,
        {"error": "lease token refused"},
    )
    assert post(lab, signer, "/heartbeat", {"job": grant["job"], "token": grant["token"]}) == (
        200,
        {"expires_ms": 1700},
    )
    assert post(lab, signer, "/complete", {"job": grant["job"], "token": "", "outcome": "passed"}) == (
        403,
        {"error": "lease token refused"},
    )
    assert post(lab, signer, "/complete", {"job": grant["job"], "token": grant["token"], "outcome": "passed"}) == (
        200,
        {"completed": True},
    )
    assert jobs.job(grant["job"]).outcome == "passed"
    assert post(lab, signer, "/complete", {"job": grant["job"], "token": grant["token"], "outcome": "passed"}) == (
        403,
        {"error": "lease token refused"},
    )


def test_upload_url_needs_the_token(lab: leases.Leases, signer: Signer, jobs: queue.Queue) -> None:
    grant = granted(lab, signer, jobs)
    job = grant["job"]

    assert lease_api.respond(lab, signer, "GET", f"/upload-url?job={job}&token=wrong&name=report", b"") == (
        403,
        {"error": "lease token refused"},
    )
    assert lease_api.respond(lab, signer, "GET", f"/upload-url?job={job}&name=log", b"") == (
        403,
        {"error": "lease token refused"},
    )
    assert signer.calls == []
    assert lease_api.respond(lab, signer, "GET", f"/upload-url?job={job}&token={grant['token']}&name=log", b"") == (
        200,
        {"url": f"https://objects.test/{job}/log?signed"},
    )
    assert signer.calls == [(job, "log")]
    assert lease_api.respond(lab, signer, "GET", f"/upload-url?job={job}&token={grant['token']}&name=other", b"") == (
        400,
        {"error": "upload name must be report or log"},
    )


@pytest.mark.parametrize(
    ("method", "path", "body", "expected"),
    [
        ("GET", "/lease", b"", (404, {"error": "not found"})),
        ("POST", "/upload-url", b"{}", (404, {"error": "not found"})),
        ("POST", "/nowhere", b"{}", (404, {"error": "not found"})),
        ("POST", "/lease", b"not json", (400, {"error": "bad request"})),
        ("POST", "/lease", b"[]", (400, {"error": "bad request"})),
        ("POST", "/heartbeat", b'{"job": "x"}', (400, {"error": "bad request"})),
        (
            "POST",
            "/lease",
            b'{"worker": "w", "free_slots": "2", "capabilities": [], "wait_ms": 0}',
            (400, {"error": "bad request"}),
        ),
    ],
)
def test_bad_requests(lab: leases.Leases, signer: Signer, method: str, path: str, body: bytes, expected: tuple) -> None:
    assert lease_api.respond(lab, signer, method, path, body) == expected


def test_server_round_trip(lab: leases.Leases, signer: Signer, jobs: queue.Queue) -> None:
    jobs.enqueue([Unit(kind="test", key="a", members=())], "h1", {})
    server = lease_api.serve("127.0.0.1", 0, lab, signer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        request = urllib.request.Request(
            f"{base}/lease",
            data=json.dumps({"worker": "w1", "free_slots": 1, "capabilities": [], "wait_ms": 0}).encode(),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            assert response.headers["Content-Type"] == "application/json"
            grant = json.loads(response.read())["leases"][0]
        assert grant["job"] == "h1:test:a"
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(f"{base}/upload-url?job=h1:test:a&token=wrong&name=log", timeout=10)
        assert refused.value.code == 403
        assert json.loads(refused.value.read()) == {"error": "lease token refused"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(10)
