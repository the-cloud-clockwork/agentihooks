import json
import threading
import urllib.error
import urllib.request
from typing import Any

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


def found(jobs: queue.Queue, job: str) -> queue.Job:
    record = jobs.job(job)
    assert record is not None
    return record


def post(lab: leases.Leases, signer: Signer, path: str, body: object) -> tuple[int, dict[str, Any]]:
    return lease_api.respond(lab, signer, "POST", path, json.dumps(body).encode())


def granted(lab: leases.Leases, signer: Signer, jobs: queue.Queue) -> dict[str, Any]:
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


WORD = "lab" * 6
BEARER = f"Bearer {WORD}"


class Control:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def prioritize(self, heads: frozenset[str]) -> None:
        self.calls.append(("prioritize", *sorted(heads)))

    def pause(self, name: str, spared: frozenset[str]) -> None:
        self.calls.append(("pause", name, *sorted(spared)))

    def resume(self) -> list[str]:
        self.calls.append(("resume",))
        return ["pull_request"]


def govern(
    control: Control, path: str, body: dict[str, Any], authorization: str = BEARER, word: str = WORD
) -> tuple[int, dict[str, Any]]:
    return lease_api.govern(control, word, "POST", path, authorization, json.dumps(body).encode())


@pytest.mark.feature("lab-control-routes")
def test_priority_gives_the_heads_the_hotfix_class() -> None:
    control = Control()
    reply = govern(control, "/priority", {"heads": ["fix-branch", "abc"], "class": "hotfix"})
    assert reply == (200, {"class": "hotfix", "heads": ["abc", "fix-branch"]})
    assert control.calls == [("prioritize", "abc", "fix-branch")]


@pytest.mark.feature("lab-control-routes")
def test_pause_holds_a_class_except_the_named_heads() -> None:
    control = Control()
    reply = govern(control, "/pause", {"class": "pull_request", "except": ["fix-branch"]})
    assert reply == (200, {"paused": "pull_request", "except": ["fix-branch"]})
    assert control.calls == [("pause", "pull_request", "fix-branch")]


@pytest.mark.feature("lab-control-routes")
def test_resume_releases_the_paused_classes() -> None:
    control = Control()
    assert govern(control, "/resume", {}) == (200, {"resumed": ["pull_request"]})
    assert control.calls == [("resume",)]


@pytest.mark.feature("lab-control-routes")
@pytest.mark.parametrize(
    ("authorization", "word"),
    [("", WORD), ("Bearer wrong", WORD), (WORD, WORD), (BEARER, ""), ("Bearer ", "")],
)
@pytest.mark.parametrize("path", ["/priority", "/pause", "/resume"])
def test_control_refuses_a_request_without_the_lab_token(authorization: str, word: str, path: str) -> None:
    control = Control()
    body = {"heads": ["a"], "class": "hotfix", "except": []}
    assert govern(control, path, body, authorization, word) == (401, {"error": "lab control token refused"})
    assert control.calls == []


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/priority", {"heads": ["a"], "class": "pull_request"}),
        ("/priority", {"heads": "a", "class": "hotfix"}),
        ("/priority", {"heads": ["a"]}),
        ("/pause", {"class": "nightly", "except": []}),
        ("/pause", {"class": "pull_request"}),
        ("/pause", {"except": []}),
    ],
)
def test_control_refuses_a_malformed_body(path: str, body: dict[str, Any]) -> None:
    control = Control()
    assert govern(control, path, body) == (400, {"error": "bad request"})
    assert control.calls == []


def test_control_answers_not_found_off_its_routes() -> None:
    control = Control()
    assert lease_api.govern(control, WORD, "GET", "/pause", BEARER, b"{}") == (404, {"error": "not found"})
    assert control.calls == []


def _post(base: str, path: str, body: dict[str, Any], headers: dict[str, str]) -> tuple[int, Any]:
    request = urllib.request.Request(f"{base}{path}", data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


@pytest.mark.feature("lab-control-routes")
def test_server_routes_control_requests_with_the_bearer_token(lab: leases.Leases, signer: Signer) -> None:
    control = Control()
    server = lease_api.serve("127.0.0.1", 0, lab, signer, control, WORD)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        paused = _post(base, "/pause", {"class": "pull_request", "except": ["fix"]}, {"Authorization": BEARER})
        assert paused == (200, {"paused": "pull_request", "except": ["fix"]})
        refused = _post(base, "/priority", {"heads": ["fix"], "class": "hotfix"}, {})
        assert refused == (401, {"error": "lab control token refused"})
        lease = {"worker": "w1", "free_slots": 1, "capabilities": [], "wait_ms": 0}
        assert _post(base, "/lease", lease, {"Authorization": BEARER}) == (200, {"leases": []})
        assert control.calls == [("pause", "pull_request", "fix")]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(10)


def test_server_without_control_answers_not_found_on_control_routes(lab: leases.Leases, signer: Signer) -> None:
    server = lease_api.serve("127.0.0.1", 0, lab, signer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert _post(base, "/resume", {}, {"Authorization": BEARER}) == (404, {"error": "not found"})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(10)
