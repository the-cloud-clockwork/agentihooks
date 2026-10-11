import json
from pathlib import Path

import pytest

from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2 import telemetry
from scripts.swarm_v2.api import health

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-OBS-04"
SLUG = "fixture"
NOW = 1_000_000
LIMITS = health.Limits(fresh_ms=1000, stall_ms=5000, report_ms=300, brain_interval_ms=100)


class Clock:
    def __init__(self, ms: int = NOW) -> None:
        self.ms = ms

    def __call__(self) -> float:
        return self.ms / 1000


class Probe:
    def __init__(self, answer: str = "ok") -> None:
        self.answer, self.calls = answer, 0

    def __call__(self) -> str:
        self.calls += 1
        return self.answer


class Held:
    epoch = 3


class Controller:
    def __init__(self, held=Held()) -> None:
        self.held = held


@pytest.fixture
def server():
    import fakeredis

    return fakeredis.FakeServer()


@pytest.fixture
def store(server, monkeypatch):
    import fakeredis

    clock = Clock()
    monkeypatch.setattr("time.time", clock)
    found = RedisStore(fakeredis.FakeRedis(server=server, decode_responses=True))
    found.create(SwarmConfig(SLUG, "agentihooks", 2, 0))
    found.clock = clock
    return found


def make(store, probe=None, controller=None, limits=LIMITS):
    return health.Health(store, SLUG, probe or Probe(), controller, limits, store.clock)


def occupant(store, seat="eng-1@fixture", task="task") -> AgentRecord:
    from scripts.swarm_v2.controller import Controller as Admitting

    admitting = Admitting(store, SLUG, [], lambda: True)
    assert admitting.acquire()
    return admitting.admit(AgentRecord(store.next_name(SLUG, "eng"), "eng", task, seat=seat))


def heartbeat(store, agent, accepted_at_ms, watermark=10, state="working", advanced_at_ms=None, **extra):
    record = {
        "renewal_sequence": 1,
        "state": state,
        "archive_watermark": watermark,
        "accepted_at_ms": accepted_at_ms,
        **extra,
    }
    if advanced_at_ms is not None:
        record["archive_advanced_at_ms"] = advanced_at_ms
    store.redis.hset(store.key(SLUG, "heartbeats"), agent.execution_id, json.dumps(record))


def test_liveness_is_shallow_and_reads_no_dependency(store, server):
    probe = Probe()
    checker = make(store, probe, Controller())
    server.connected = False
    store.clock.ms += 250
    assert checker.live() == {
        "probe": "shallow",
        "status": "live",
        "pid": checker.pid,
        "started_at_ms": NOW,
        "checked_at_ms": NOW + 250,
        "qualifies": [],
    }
    assert probe.calls == 0


@pytest.mark.parametrize(
    ("answer", "state"),
    [
        ("ok", "ok"),
        ("unconfigured", "unconfigured"),
        ("brain_http_401", "auth_failed"),
        ("brain_http_403", "auth_failed"),
        ("brain_unreachable", "unavailable"),
        ("brain_http_500", "error"),
    ],
)
def test_a_brain_answer_is_classified(answer, state):
    assert health.classify_brain(answer) == state


def test_a_dependency_reports_its_last_successful_check(store, server):
    checker = make(store, Probe(), Controller())
    first = checker.diagnose()["dependencies"]
    assert first == {
        "redis": {"state": "ok", "reason": "", "checked_at_ms": NOW, "last_ok_at_ms": NOW},
        "brain": {"state": "ok", "reason": "ok", "checked_at_ms": NOW, "last_ok_at_ms": NOW},
    }
    server.connected = False
    store.clock.ms += 50
    redis = checker.diagnose()["dependencies"]["redis"]
    assert (redis["state"], redis["checked_at_ms"], redis["last_ok_at_ms"]) == ("unavailable", NOW + 50, NOW)
    assert redis["reason"].startswith("ConnectionError: ")


def test_the_brain_is_probed_at_most_once_per_interval(store):
    probe = Probe("brain_http_401")
    checker = make(store, probe, Controller())
    checker.diagnose()
    store.clock.ms += LIMITS.brain_interval_ms - 1
    brain = checker.diagnose()["dependencies"]["brain"]
    assert (probe.calls, brain["checked_at_ms"], brain["last_ok_at_ms"]) == (1, NOW, 0)
    store.clock.ms += 1
    probe.answer = "ok"
    brain = checker.diagnose()["dependencies"]["brain"]
    assert (probe.calls, brain["state"], brain["last_ok_at_ms"]) == (2, "ok", NOW + LIMITS.brain_interval_ms)


@pytest.mark.parametrize(
    ("held", "lease"),
    [(Controller(), "held"), (Controller(None), "absent"), (None, "unmanaged")],
)
def test_the_process_section_reads_the_local_lease_only(store, held, lease):
    process = make(store, Probe(), held).diagnose()["process"]
    assert process == {"status": "live", "pid": process["pid"], "started_at_ms": NOW, "controller_lease": lease}


@pytest.mark.parametrize(
    ("state", "advanced", "beat", "expected", "process"),
    [
        ("working", NOW - 5001, NOW, "stalled", "live"),
        ("working", NOW - 5000, NOW, "ok", "live"),
        ("waiting", NOW - 9000, NOW, "ok", "live"),
        ("working", NOW - 9000, NOW - 1001, "unknown", "silent"),
        ("working", NOW - 9000, NOW - 1000, "stalled", "live"),
    ],
)
def test_the_archive_stage_separates_process_liveness_from_watermark_progress(
    store, state, advanced, beat, expected, process
):
    agent = occupant(store)
    heartbeat(store, agent, beat, 77, state, advanced)
    entry = make(store).diagnose()["pipeline"]["archive"][0]
    assert entry == {
        "execution_id": agent.execution_id,
        "seat": agent.seat,
        "process": process,
        "activity": state,
        "heartbeat_at_ms": beat,
        "watermark": 77,
        "advanced_at_ms": advanced,
        "state": expected,
        "observed_at_ms": NOW,
    }


def test_a_heartbeat_without_an_advance_time_counts_from_its_acceptance(store):
    agent = occupant(store)
    heartbeat(store, agent, NOW - 10)
    entry = make(store).diagnose()["pipeline"]["archive"][0]
    assert (entry["advanced_at_ms"], entry["state"]) == (NOW - 10, "ok")


def test_an_occupant_without_a_heartbeat_has_no_archive_entry(store):
    occupant(store)
    assert make(store).diagnose()["pipeline"]["archive"] == []


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ({}, {"state": "disabled", "watermark": 4, "backlog": 0}),
        ({"state": "ok"}, {"state": "ok", "watermark": 4, "backlog": 2}),
        ({"state": "degraded"}, {"state": "degraded", "watermark": 4, "backlog": 2}),
    ],
)
def test_the_transition_export_stage_reports_its_durable_watermark(store, record, expected):
    store.redis.set(store.key(SLUG, telemetry.SEQUENCE), 6)
    store.redis.set(store.key(SLUG, telemetry.EXPORTED), 4)
    if record:
        store.redis.hset(store.key(SLUG, telemetry.OBSERVABILITY), mapping=record)
    stage = make(store).diagnose()["pipeline"]["transition_export"]
    assert {name: stage[name] for name in expected} == expected
    assert stage["observed_at_ms"] == NOW


def test_remediation_is_classified_ordered_and_never_automatic(store, server):
    agent = occupant(store)
    heartbeat(store, agent, NOW, 5, "working", NOW - 6000)
    store.redis.hset(store.key(SLUG, telemetry.OBSERVABILITY), "state", "degraded")
    checker = make(store, Probe("brain_unreachable"), Controller())
    report = checker.diagnose()
    assert [(h["component"], h["condition"], h["class"]) for h in report["remediation"]] == [
        ("brain", "unavailable", "dependency_unavailable"),
        ("transition_export", "degraded", "pipeline_degraded"),
        ("archive", "stalled", "pipeline_stalled"),
    ]
    assert report["remediation"][2]["execution_id"] == agent.execution_id
    assert all(hint["automatic"] is False and hint["hint"] for hint in report["remediation"])
    assert report["status"] == "degraded"
    server.connected = False
    assert [h["class"] for h in checker.diagnose()["remediation"]][0] == "dependency_unavailable"


def test_a_clean_swarm_reads_ok(store):
    agent = occupant(store)
    heartbeat(store, agent, NOW, 5, "working", NOW)
    report = make(store, Probe(), Controller()).diagnose()
    assert (report["status"], report["remediation"], report["scope"]) == ("ok", [], "swarm")


def test_an_unconfigured_brain_does_not_degrade_the_report(store):
    assert make(store, Probe("unconfigured"), Controller()).diagnose()["status"] == "ok"


def test_an_execution_scope_sees_only_its_own_archive_entry(store):
    mine, other = occupant(store), occupant(store, "eng-2@fixture", "other")
    heartbeat(store, mine, NOW)
    heartbeat(store, other, NOW, 5, "working", NOW - 9000)
    report = make(store, Probe(), Controller()).diagnose(mine.execution_id)
    assert report["scope"] == "execution"
    assert [entry["execution_id"] for entry in report["pipeline"]["archive"]] == [mine.execution_id]
    assert "transition_export" not in report["pipeline"]
    assert report["remediation"] == []


def test_a_redis_outage_reports_the_last_reading_as_unknown(store, server):
    agent = occupant(store)
    heartbeat(store, agent, NOW, 9, "working", NOW - 9000)
    checker = make(store)
    checker.diagnose()
    server.connected = False
    store.clock.ms += 20
    report = checker.diagnose()
    entry = report["pipeline"]["archive"][0]
    assert (entry["state"], entry["last_observed_state"], entry["observed_at_ms"], entry["watermark"]) == (
        "unknown",
        "stalled",
        NOW,
        9,
    )
    assert report["pipeline"]["transition_export"]["state"] == "unknown"


def test_a_redis_outage_before_any_reading_reports_no_stage(store, server):
    server.connected = False
    report = make(store).diagnose()
    assert report["pipeline"] == {"archive": [], "transition_export": {"state": "unknown"}}


def test_a_dependency_error_is_sanitized(store, monkeypatch):
    secret = "Bear" + "er abcdefghijklmnop"

    def refuse():
        raise ConnectionError(f"refused with {secret}")

    monkeypatch.setattr(store.redis, "ping", refuse)
    reason = make(store).diagnose()["dependencies"]["redis"]["reason"]
    assert reason == "ConnectionError: refused with [redacted]"


def test_only_the_last_issued_reports_are_recognized(store):
    checker = make(store)
    first = checker.diagnose()
    for _ in range(health.ISSUED_LIMIT):
        checker.diagnose()
    with pytest.raises(health.HealthRefused) as refused:
        checker.qualify(first, "backup")
    assert refused.value.reason == "unrecognized_report"


def test_a_recent_report_qualifies_backup(store):
    agent = occupant(store)
    heartbeat(store, agent, NOW, 5, "working", NOW)
    checker = make(store)
    report = checker.diagnose()
    store.clock.ms += LIMITS.report_ms
    assert checker.qualify(report, "backup") == {
        "purpose": "backup",
        "qualified": True,
        "execution_id": "",
        "report_id": report["report_id"],
        "checked_at_ms": NOW,
    }


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda report: report.pop("report_id"), "unrecognized_report"),
        (lambda report: report.update(probe="shallow"), "shallow_report"),
        (lambda report: report.update(status="forged"), "unrecognized_report"),
    ],
)
def test_an_altered_report_is_refused(store, mutate, reason):
    checker = make(store)
    report = checker.diagnose()
    mutate(report)
    with pytest.raises(health.HealthRefused) as refused:
        checker.qualify(report, "backup")
    assert (refused.value.reason, str(refused.value)) == (reason, health.REFUSALS[reason])


def test_backup_needs_redis(store, server):
    checker = make(store)
    server.connected = False
    report = checker.diagnose()
    with pytest.raises(health.HealthRefused) as refused:
        checker.qualify(report, "backup")
    assert refused.value.reason == "dependency_unavailable"


def test_native_resume_needs_the_named_execution_entry(store):
    checker = make(store)
    report = checker.diagnose()
    with pytest.raises(health.HealthRefused) as refused:
        checker.qualify(report, "native_resume", "exe-missing")
    assert refused.value.reason == "pipeline_not_ok"


@pytest.mark.parametrize(("value", "expected"), [("off", False), ("OFF", False), ("on", True), ("", True)])
def test_deep_checks_switch_off_only_by_the_setting(value, expected):
    assert health.deep_enabled({health.DEEP_ENV: value}) is expected


def test_pipeline_stage_health_counts_states_per_stage():
    report = {
        "pipeline": {
            "transition_export": {"state": "ok"},
            "archive": [{"state": "ok"}, {"state": "stalled"}, {"state": "ok"}],
        }
    }
    assert health.pipeline_stage_health(report) == {
        "archive": {"ok": 2, "stalled": 1},
        "transition_export": {"ok": 1},
    }


class Grants:
    def __init__(self, execution_id="exe-eng-1"):
        self.execution_id = execution_id

    def bound(self, slug, token):
        from scripts.swarm_v2.auth_context import GrantRefused

        if token != "worker":
            raise GrantRefused("unauthenticated", "launch grant is not registered")
        return type("Registration", (), {"execution_id": self.execution_id})()


def operator(slug, credential):
    from scripts.swarm_v2.operator_auth import OPERATOR

    return OPERATOR if credential == "operator" else None


@pytest.fixture
def api(store):
    return health.HealthAPI(make(store, Probe(), Controller()), Grants(), operator)


def test_the_live_route_needs_no_credential(api):
    status, reply = api.route("GET", health.LIVE_PATH, "None", None)
    assert (status, reply["probe"]) == (200, "shallow")


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", health.LIVE_PATH), ("GET", "/v2/health/other"), ("GET", health.QUALIFY_PATH)],
)
def test_an_unknown_health_route_is_not_found(api, method, path):
    assert api.route(method, path, "Bearer operator", None)[0] == 404


@pytest.mark.parametrize(
    ("authorization", "status", "error"),
    [("None", 401, "unauthenticated"), ("Bearer stranger", 401, "unauthenticated")],
)
def test_diagnostics_need_a_credential(api, authorization, status, error):
    found, reply = api.route("GET", health.DIAGNOSE_PATH, authorization, None)
    assert (found, reply["error_class"]) == (status, error)


def test_a_worker_grant_reads_its_own_execution_scope(api):
    status, reply = api.route("GET", health.DIAGNOSE_PATH, "Bearer worker", None)
    assert (status, reply["scope"]) == (200, "execution")


def test_an_unreachable_operator_credential_store_answers_unavailable(store):
    def broken(slug, credential):
        raise OSError("ledger store unreachable")

    api = health.HealthAPI(make(store), Grants(), broken)
    status, reply = api.route("GET", health.DIAGNOSE_PATH, "Bearer operator", None)
    assert (status, reply["error_class"], reply["message"]) == (
        503,
        "dependency_unavailable",
        "the credential store is unavailable",
    )


def test_a_missing_grant_authority_refuses_a_non_operator_credential(store):
    api = health.HealthAPI(make(store), None, operator)
    status, reply = api.route("GET", health.DIAGNOSE_PATH, "Bearer worker", None)
    assert (status, reply["message"]) == (401, "the credential is not recognized")


def test_switched_off_deep_checks_answer_unavailable_and_keep_liveness(store):
    api = health.HealthAPI(make(store), Grants(), operator, deep=False)
    status, reply = api.route("GET", health.DIAGNOSE_PATH, "Bearer operator", None)
    assert (status, reply["error_class"], reply["message"]) == (
        503,
        "dependency_unavailable",
        "deep diagnostics are switched off",
    )
    assert api.route("GET", health.LIVE_PATH, "None", None)[0] == 200


@pytest.mark.parametrize("body", [None, [], {"purpose": "backup"}, {"purpose": 1, "report": {}}])
def test_a_malformed_qualify_request_is_invalid(api, body):
    status, reply = api.route("POST", health.QUALIFY_PATH, "Bearer operator", body)
    assert (status, reply["error_class"]) == (400, "invalid_request")


def test_qualify_answers_the_gate_decision(api):
    report = api.health.diagnose()
    status, reply = api.route("POST", health.QUALIFY_PATH, "Bearer operator", {"purpose": "backup", "report": report})
    assert (status, reply["qualified"]) == (200, True)


def test_routes_send_health_paths_to_the_health_api(api):
    from scripts.swarm_v2.api.server import Routes

    routes = Routes(None, None, health=api)
    assert routes.route("GET", health.LIVE_PATH, "None", None)[0] == 200


def test_routes_without_a_tasks_api_answer_task_paths_from_executions():
    from scripts.swarm_v2.api.server import Routes

    class Executions:
        def route(self, method, path, authorization, body):
            return 404, {"path": path}

    assert Routes(Executions(), None).route("GET", "/v2/tasks/x", "None", None) == (404, {"path": "/v2/tasks/x"})


def test_the_heartbeat_records_when_the_archive_watermark_last_advanced(monkeypatch):
    from tests.sv2_ldg02_cases import World

    world = World(monkeypatch)
    agent, token = world.start()
    assert world.register(agent, token)[0] == 200
    started = world.clock[0]
    world.put(agent.execution_id, token, world.beat(agent, 1, archive_watermark=5))
    world.clock[0] += 10
    world.put(agent.execution_id, token, world.beat(agent, 2, archive_watermark=5))
    world.clock[0] += 10
    world.put(agent.execution_id, token, world.beat(agent, 3, archive_watermark=3))
    raw = world.store.redis.hget(world.store.key("fixture", "heartbeats"), agent.execution_id)
    assert json.loads(raw)["archive_advanced_at_ms"] == started
    world.clock[0] += 10
    world.put(agent.execution_id, token, world.beat(agent, 4, archive_watermark=6))
    raw = world.store.redis.hget(world.store.key("fixture", "heartbeats"), agent.execution_id)
    assert json.loads(raw)["archive_advanced_at_ms"] == started + 30


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    from tests import sv2_obs04_cases as cases

    first = cases.run_case(case)
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text()) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True)
