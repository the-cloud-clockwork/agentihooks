import json
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from functools import partial
from typing import Any

import pytest

from scripts.ci_lab import lease_api, leases, queue, service
from scripts.ci_lab.capacity import Load, document
from scripts.ci_lab.units import Unit

HEAD = "a" * 40
LONG_HEAD = "b" * 64
LOAD = Load(pending={"pull_request": (120.0, 60.0)}, running={"queue": 1}, slots_total=3, slots_busy=1)


class Signer:
    def upload_url(self, job: str, name: str) -> str:
        return f"https://objects.test/{job}/{name}"


@pytest.fixture
def jobs() -> queue.Queue:
    import fakeredis

    return queue.Queue(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))


@pytest.fixture
def routes(jobs: queue.Queue) -> service.Routes:
    lab = leases.Leases(jobs, ttl_ms=500, clock=lambda: 1000, sleep=lambda ms: None, poll_ms=50)

    def grades(head: str) -> list[dict[str, Any]]:
        return [{"run": f"run-of-{head}"}] if head == HEAD else []

    return service.Routes(partial(lease_api.answer, lab, Signer()), lambda: LOAD, grades)


@pytest.mark.feature("lab-scheduler-service")
def test_the_scheduler_port_answers_capacity_from_the_load_snapshot(routes: service.Routes) -> None:
    assert routes.respond("GET", "/capacity", b"", None) == (200, document(LOAD))
    assert routes.respond("GET", "/capacity?fresh=1", b"", None) == (200, document(LOAD))


@pytest.mark.feature("lab-scheduler-first-build-at-bind")
def test_the_scheduler_port_answers_capacity_not_built_before_the_first_build(routes: service.Routes) -> None:
    unbuilt = replace(routes, snapshot=lambda: None)
    assert unbuilt.respond("GET", "/capacity", b"", None) == (503, {"error": "the load snapshot is not built yet"})
    assert unbuilt.respond("GET", f"/grades/{HEAD}", b"", None) == (
        200,
        {"head": HEAD, "grades": [{"run": f"run-of-{HEAD}"}]},
    )


@pytest.mark.feature("lab-scheduler-service")
def test_the_scheduler_port_answers_every_grade_of_a_commit(routes: service.Routes) -> None:
    assert routes.respond("GET", f"/grades/{HEAD}", b"", None) == (
        200,
        {"head": HEAD, "grades": [{"run": f"run-of-{HEAD}"}]},
    )
    assert routes.respond("GET", f"/grades/{LONG_HEAD}", b"", None) == (200, {"head": LONG_HEAD, "grades": []})


@pytest.mark.feature("lab-scheduler-service")
@pytest.mark.parametrize("head", ["", "a" * 39, "a" * 41, "A" * 40, "g" * 40, f"{HEAD}/x", "../" + "a" * 37])
def test_a_grade_lookup_refuses_anything_but_one_full_commit_sha(routes: service.Routes, head: str) -> None:
    assert routes.respond("GET", f"/grades/{head}", b"", None) == (
        400,
        {"error": "a grade lookup names one full commit sha"},
    )


@pytest.mark.feature("lab-scheduler-service")
def test_the_scheduler_port_keeps_every_lease_route(routes: service.Routes, jobs: queue.Queue) -> None:
    [job] = jobs.enqueue([Unit("static", "k", ("lint",))], HEAD, {})
    body = json.dumps({"worker": "w", "free_slots": 1, "capabilities": [], "wait_ms": 0}).encode()
    status, reply = routes.respond("POST", "/lease", body, None)
    assert status == 200 and [grant["job"] for grant in reply["leases"]] == [job]
    assert routes.respond("POST", "/capacity", b"", None) == (404, {"error": "not found"})
    assert routes.respond("POST", f"/grades/{HEAD}", b"", None) == (404, {"error": "not found"})
    assert routes.respond("GET", "/missing", b"", None) == (404, {"error": "not found"})
    assert routes.respond("POST", "/resume", b"", None) == (404, {"error": "not found"})
    assert routes.respond("POST", "/resume", b"", "Bearer tok") == (404, {"error": "not found"})


@pytest.mark.feature("lab-scheduler-service")
def test_the_scheduler_port_serves_its_routes_over_http(routes: service.Routes) -> None:
    server = service.serve("127.0.0.1", 0, routes)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/capacity", timeout=5) as reply:
            assert reply.headers["Content-Type"] == "application/json"
            assert json.loads(reply.read()) == json.loads(json.dumps(document(LOAD)))
        with urllib.request.urlopen(f"{base}/grades/{HEAD}", timeout=5) as reply:
            assert json.loads(reply.read()) == {"head": HEAD, "grades": [{"run": f"run-of-{HEAD}"}]}
        body = json.dumps({"worker": "w", "free_slots": 1, "capabilities": [], "wait_ms": 0}).encode()
        with urllib.request.urlopen(urllib.request.Request(f"{base}/lease", data=body), timeout=5) as reply:
            assert json.loads(reply.read()) == {"leases": []}
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(f"{base}/grades/nope", timeout=5)
        assert refused.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
