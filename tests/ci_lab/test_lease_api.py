import http.client
import json
import threading
import urllib.error
import urllib.request
from typing import Any

import pytest

from scripts.ci_lab import control, lease_api, leases, queue
from scripts.ci_lab.units import Unit

TOKEN = "control-token"
BEARER = f"Bearer {TOKEN}"


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
        assert ms > 0
        assert len(self.sleeps) < 1000
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


@pytest.fixture
def routes(clock: Clock) -> lease_api.ControlRoutes:
    import fakeredis

    return lease_api.ControlRoutes(
        fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True), TOKEN, clock
    )


def found(jobs: queue.Queue, job: str) -> queue.Job:
    record = jobs.job(job)
    assert record is not None
    return record


def post(lab: leases.Leases, signer: Signer, path: str, body: object) -> tuple[int, dict[str, Any]]:
    return lease_api.respond(lab, signer, "POST", path, json.dumps(body).encode())


def granted(lab: leases.Leases, signer: Signer, jobs: queue.Queue) -> dict[str, Any]:
    jobs.enqueue([Unit(kind="test", key="a", members=("tests/a.py::t",))], "h1", {}, "b1", "g1")
    status, body = post(lab, signer, "/lease", {"worker": "w1", "free_slots": 1, "capabilities": [], "wait_ms": 0})
    assert status == 200
    return body["leases"][0]


@pytest.mark.feature("lab-worker-executors")
def test_lease_returns_the_grant_payload(lab: leases.Leases, signer: Signer, jobs: queue.Queue) -> None:
    grant = granted(lab, signer, jobs)

    token = grant.pop("token")
    assert isinstance(token, str) and len(token) == 32
    assert grant == {
        "job": "h1:test:a",
        "head": "h1",
        "base": "b1",
        "grader": "g1",
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
    assert found(jobs, grant["job"]).outcome == "passed"
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
    assert lease_api.respond(lab, signer, "GET", f"/upload-url?token={grant['token']}&name=log", b"") == (
        403,
        {"error": "lease token refused"},
    )
    assert lease_api.respond(lab, signer, "GET", f"/upload-url?job={job}&token={grant['token']}", b"") == (
        400,
        {"error": "upload name must be report or log"},
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


@pytest.mark.parametrize(
    "body",
    [
        {"worker": "w", "free_slots": True, "capabilities": [], "wait_ms": 0},
        {"worker": "w", "free_slots": 1, "capabilities": ["net", 3], "wait_ms": 0},
        {"worker": "w", "free_slots": 1, "capabilities": "net", "wait_ms": 0},
        {"worker": 4, "free_slots": 1, "capabilities": [], "wait_ms": 0},
    ],
)
def test_lease_refuses_malformed_fields(lab: leases.Leases, signer: Signer, body: dict[str, Any]) -> None:
    assert post(lab, signer, "/lease", body) == (400, {"error": "bad request"})


def test_upload_url_serves_the_report(lab: leases.Leases, signer: Signer, jobs: queue.Queue) -> None:
    grant = granted(lab, signer, jobs)
    target = f"/upload-url?job={grant['job']}&token={grant['token']}&name=report"

    assert lease_api.respond(lab, signer, "GET", target, b"") == (
        200,
        {"url": f"https://objects.test/{grant['job']}/report?signed"},
    )


@pytest.mark.parametrize("length", ["abc", "-1", "²", str(lease_api.MAX_BODY_BYTES + 1)])
def test_server_refuses_a_bad_content_length(lab: leases.Leases, signer: Signer, length: str) -> None:
    import http.client

    server = lease_api.serve("127.0.0.1", 0, lab, signer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        connection.putrequest("POST", "/lease")
        connection.putheader("Content-Length", length)
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 400
        assert json.loads(response.read()) == {"error": "bad request"}
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(10)


def test_server_reads_a_body_at_the_cap(lab: leases.Leases, signer: Signer) -> None:
    import http.client

    body = json.dumps({"worker": "w1", "free_slots": 1, "capabilities": [], "wait_ms": 0}).encode()
    body += b" " * (lease_api.MAX_BODY_BYTES - len(body))
    server = lease_api.serve("127.0.0.1", 0, lab, signer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
        connection.request("POST", "/lease", body=body)
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == {"leases": []}
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(10)


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
            raw = response.read()
            assert ("Content-Type", "application/json") in response.getheaders()
            assert ("Content-Length", str(len(raw))) in response.getheaders()
            grant = json.loads(raw)["leases"][0]
        assert grant["job"] == "h1:test:a"
        signed = f"{base}/upload-url?job=h1:test:a&token={grant['token']}&name=log"
        with urllib.request.urlopen(signed, timeout=10) as response:
            assert json.loads(response.read()) == {"url": "https://objects.test/h1:test:a/log?signed"}
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(f"{base}/upload-url?job=h1:test:a&token=wrong&name=log", timeout=10)
        assert refused.value.code == 403
        assert json.loads(refused.value.read()) == {"error": "lease token refused"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(10)


@pytest.mark.feature("lab-scheduler-access-log-query-redaction")
@pytest.mark.parametrize(("method", "status"), [("GET", 200), ("GET", 403), ("POST", 200)])
@pytest.mark.parametrize(
    ("target", "logged_path"),
    [("/upload-url?job=example&token=synthetic-lease-value&name=log", "/upload-url"), ("/health", "/health")],
)
def test_access_logs_omit_upload_query_strings(
    method: str, status: int, target: str, logged_path: str, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[tuple[str, str, bytes, str | None]] = []

    def reply(method: str, target: str, raw: bytes, authorization: str | None) -> tuple[int, dict[str, Any]]:
        seen.append((method, target, raw, authorization))
        return status, {}

    server = lease_api.bind("127.0.0.1", 0, reply)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    try:
        connection.request(method, target)
        response = connection.getresponse()
        assert response.status == status
        assert json.loads(response.read()) == {}
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(10)
    assert seen == [(method, target, b"", None)]
    logged = capsys.readouterr().err
    assert "synthetic-lease-value" not in logged
    assert "?job=" not in logged
    assert f'"{method} {logged_path} HTTP/1.1" {status} -\n' in logged


def order(routes: lease_api.ControlRoutes, path: str, body: object, authorization: str = BEARER) -> object:
    return lease_api.command(routes, "POST", path, json.dumps(body).encode(), authorization)


@pytest.mark.feature("lab-control-routes")
@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/priority", {"heads": ["fix-branch"], "class": "hotfix"}),
        ("/pause", {"class": "pull_request", "except": []}),
        ("/resume", {}),
    ],
    ids=["priority", "pause", "resume"],
)
@pytest.mark.parametrize("authorization", ["", TOKEN, f"Bearer {TOKEN}x", f"bearer {TOKEN}", f"Bearer  {TOKEN}"])
def test_a_control_route_refuses_any_other_authorization(
    routes: lease_api.ControlRoutes, path: str, body: object, authorization: str
) -> None:
    control.pause(routes.client, "backfill", [])
    assert order(routes, path, body, authorization) == (401, {"error": "control token refused"})
    assert control.paused(routes.client) == {"backfill": frozenset()}
    assert control.prioritized(routes.client, 0) == frozenset()


@pytest.mark.feature("lab-control-routes")
def test_a_lab_without_a_control_token_refuses_every_control_request(clock: Clock) -> None:
    import fakeredis

    routes = lease_api.ControlRoutes(fakeredis.FakeRedis(decode_responses=True), "", clock)
    assert order(routes, "/resume", {}, "Bearer ") == (401, {"error": "control token refused"})
    assert order(routes, "/resume", {}, "") == (401, {"error": "control token refused"})


@pytest.mark.feature("lab-control-routes")
def test_priority_marks_the_heads_hotfix_until_they_expire(routes: lease_api.ControlRoutes, clock: Clock) -> None:
    body = {"heads": ["fix-branch", "c0ffee", "fix-branch"], "class": "hotfix"}
    expires = clock.now + control.PRIORITY_MS
    assert order(routes, "/priority", body) == (
        200,
        {"priority": ["c0ffee", "fix-branch"], "class": "hotfix", "expires_ms": expires},
    )
    assert control.prioritized(routes.client, expires - 1) == frozenset({"c0ffee", "fix-branch"})
    assert control.prioritized(routes.client, expires) == frozenset()
    clock.now = expires
    assert order(routes, "/priority", {"heads": ["next"], "class": "hotfix"}) == (
        200,
        {"priority": ["next"], "class": "hotfix", "expires_ms": expires + control.PRIORITY_MS},
    )
    assert routes.client.zrange(control.PRIORITY, 0, -1) == ["next"]


@pytest.mark.feature("lab-control-routes")
@pytest.mark.parametrize(
    "body",
    [
        {"heads": ["fix-branch"], "class": "pull_request"},
        {"heads": ["fix-branch"]},
        {"heads": "fix-branch", "class": "hotfix"},
        {"heads": [1], "class": "hotfix"},
        ["fix-branch"],
    ],
    ids=["other-class", "no-class", "heads-text", "heads-number", "not-a-map"],
)
def test_priority_refuses_anything_but_hotfix_heads(routes: lease_api.ControlRoutes, body: object) -> None:
    assert order(routes, "/priority", body) == (400, {"error": "bad request"})
    assert routes.client.keys("*") == []


@pytest.mark.feature("lab-control-routes")
def test_pause_holds_a_class_with_its_exceptions_until_resume(routes: lease_api.ControlRoutes) -> None:
    assert order(routes, "/pause", {"class": "pull_request", "except": ["fix-branch", "c0ffee"]}) == (
        200,
        {"paused": "pull_request", "except": ["c0ffee", "fix-branch"]},
    )
    assert order(routes, "/pause", {"class": "backfill", "except": []}) == (200, {"paused": "backfill", "except": []})
    assert control.paused(routes.client) == {
        "pull_request": frozenset({"c0ffee", "fix-branch"}),
        "backfill": frozenset(),
    }
    assert order(routes, "/resume", {}) == (200, {"resumed": ["backfill", "pull_request"]})
    assert control.paused(routes.client) == {}
    assert lease_api.command(routes, "POST", "/resume", b"", BEARER) == (200, {"resumed": []})


@pytest.mark.feature("lab-control-routes")
@pytest.mark.parametrize(
    "body",
    [{"class": "nightly", "except": []}, {"class": "pull_request"}, {"except": []}, {"class": "hotfix", "except": "x"}],
    ids=["unknown-class", "no-except", "no-class", "except-text"],
)
def test_pause_refuses_an_unknown_class_or_exception_list(routes: lease_api.ControlRoutes, body: object) -> None:
    assert order(routes, "/pause", body) == (400, {"error": "bad request"})
    assert routes.client.keys("*") == []


def test_a_worker_route_or_a_read_is_no_control_request(routes: lease_api.ControlRoutes) -> None:
    assert lease_api.command(routes, "POST", "/lease", b"{}", BEARER) is None
    assert lease_api.command(routes, "GET", "/priority", b"", BEARER) is None
    assert lease_api.command(routes, "POST", "/resume?now=1", b"", BEARER) == (200, {"resumed": []})


@pytest.mark.feature("lab-control-routes")
def test_the_control_port_answers_only_the_control_routes(
    lab: leases.Leases, signer: Signer, routes: lease_api.ControlRoutes
) -> None:
    assert lease_api.answer_control(routes, "POST", "/lease", b"{}", BEARER) == (404, {"error": "not found"})
    assert lease_api.answer_control(routes, "GET", "/resume", b"", BEARER) == (404, {"error": "not found"})
    assert lease_api.answer_control(routes, "POST", "/resume", b"", None) == (401, {"error": "control token refused"})
    assert lease_api.answer_control(routes, "POST", "/resume", b"", BEARER) == (200, {"resumed": []})
    assert lease_api.answer(lab, signer, "POST", "/resume", b"", BEARER) == (404, {"error": "not found"})


@pytest.mark.feature("lab-control-routes")
def test_server_answers_a_control_route_only_with_the_bearer_token(routes: lease_api.ControlRoutes) -> None:
    server = lease_api.serve_control("127.0.0.1", 0, routes)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    body = json.dumps({"class": "pull_request", "except": ["fix-branch"]}).encode()
    try:
        refused = urllib.request.Request(f"{base}/pause", data=body, method="POST")
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(refused, timeout=10)
        assert caught.value.code == 401
        assert control.paused(routes.client) == {}
        allowed = urllib.request.Request(f"{base}/pause", data=body, method="POST", headers={"Authorization": BEARER})
        with urllib.request.urlopen(allowed, timeout=10) as response:
            assert json.loads(response.read()) == {"paused": "pull_request", "except": ["fix-branch"]}
        assert control.paused(routes.client) == {"pull_request": frozenset({"fix-branch"})}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(10)
