import copy
import hashlib
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import fakeredis

from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2 import operator_auth
from scripts.swarm_v2.api import health
from scripts.swarm_v2.api.executions import ExecutionsAPI
from scripts.swarm_v2.auth_context import LaunchAuthority, LaunchKey
from scripts.swarm_v2.authority import TaskAuthority
from scripts.swarm_v2.controller import Controller

FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2"
INPUTS = ("health-diagnostics.json",)
LEASE = {"control-owner"}
EVIDENCE_CLASS = (
    "mocked: fakeredis execution registry, task claims and heartbeats behind a server that can be disconnected, a "
    "fake wall and Redis clock, signed fixture launch grants, a fixture operator page credential and an in process "
    "brain health server that answers 401 or 200; no Kubernetes API server, worker Pod, transcript exporter process "
    "or deployed brain was used"
)


def fixture() -> dict:
    return json.loads((FIXTURES / "health-diagnostics.json").read_text())


FX = fixture()
SLUG = FX["swarm"]


def operator_credential() -> str:
    return "-".join(FX["operator_credential_parts"])


def brain_token() -> str:
    return "-".join(FX["brain_token_parts"])


class Brain:
    """Answers the brain health request with 401 until restored, and records the bearer it was sent."""

    def __init__(self) -> None:
        self.rejecting = True
        self.requests = 0
        self.bearers: list[str] = []
        brain = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                brain.requests += 1
                brain.bearers.append(str(self.headers.get("Authorization")))
                self.send_response(401 if brain.rejecting else 200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def environ(self) -> dict[str, str]:
        return {"BRAIN_URL": f"http://127.0.0.1:{self.server.server_port}", "BRAIN_HTTP_TOKEN": brain_token()}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class World:
    def __init__(self) -> None:
        self.server = fakeredis.FakeServer()
        self.store = RedisStore(fakeredis.FakeRedis(server=self.server, decode_responses=True))
        self.clock = [FX["clock_ms"]]
        self.brain = Brain()

    def now(self) -> float:
        return self.clock[0] / 1000

    @contextmanager
    def clocked(self):
        with mock.patch("time.time", self.now):
            try:
                self.store.create(SwarmConfig(SLUG, "agentihooks", 2, 0))
                self.controller = Controller(self.store, SLUG, [], lambda: True)
                assert self.controller.acquire()
                self.grants = LaunchAuthority(
                    self.store, LaunchKey("fixture-key", b"x" * 32), "controller", "workers", self.now
                )
                self.tasks = TaskAuthority(self.store, self.controller, lambda token: self.grants.bound(SLUG, token))
                self.api = ExecutionsAPI(self.grants, self.tasks, FX["lease_ms"])
                self.health = health.Health(
                    self.store,
                    SLUG,
                    health.brain_probe(self.brain.environ),
                    self.controller,
                    health.Limits(**FX["limits"]),
                    self.now,
                )
                self.routes = health.HealthAPI(
                    self.health, self.grants, operator_auth.authenticator(lambda slug: operator_credential())
                )
                yield self
            finally:
                self.brain.close()

    def advance(self, ms: int) -> None:
        self.clock[0] += ms
        assert self.controller.renew()

    def start(self, run: dict) -> tuple[AgentRecord, str]:
        record = AgentRecord(self.store.next_name(SLUG, "eng"), "eng", run["task"], seat=run["seat"])
        agent = self.controller.admit(record)
        token = self.grants.issue(SLUG, agent.execution_id, project_ids=[FX["project"]], brain_id="swarm", account="x")
        body = {"execution_id": agent.execution_id, "generation": agent.generation}
        assert self.api.route("POST", "/v2/executions/register", f"Bearer {token}", body)[0] == 200
        return agent, token

    def beat(self, agent: AgentRecord, token: str, sequence: int, watermark: int) -> tuple[int, dict]:
        body = {
            "schema_version": "2.1",
            "operation_id": f"heartbeat-{sequence}",
            "authority": {
                "execution_id": agent.execution_id,
                "task_id": agent.task,
                "task_generation": 1,
                "controller_epoch": self.controller.held.epoch,
                "owner_identity": agent.name,
            },
            "renewal_sequence": sequence,
            "state": "working",
            "observed_at": "2026-10-11T12:00:00Z",
            "archive_watermark": watermark,
        }
        return self.api.route("PUT", f"/v2/executions/{agent.execution_id}/heartbeat", f"Bearer {token}", body)

    def run(self) -> dict:
        stuck, healthy = FX["stuck"], FX["healthy"]
        self.stuck, self.stuck_token = self.start(stuck)
        self.healthy, self.healthy_token = self.start(healthy)
        self.started_ms = self.clock[0]
        for sequence in range(1, stuck["beats"] + 1):
            if sequence > 1:
                self.advance(stuck["beat_every_ms"])
            assert self.beat(self.stuck, self.stuck_token, sequence, stuck["watermark"])[0] == 200
            assert self.beat(self.healthy, self.healthy_token, sequence, healthy["watermark_step"] * sequence)[0] == 200
        return {"stuck": self.stuck.execution_id, "healthy": self.healthy.execution_id}

    def get(self, path: str, credential: str = "") -> tuple[int, dict]:
        return self.routes.route("GET", path, f"Bearer {credential}" if credential else "None", None)

    def post(self, path: str, credential: str, body: object) -> tuple[int, dict]:
        return self.routes.route("POST", path, f"Bearer {credential}", body)

    def protected(self) -> dict:
        redis = self.store.redis
        reads = {"string": redis.get, "hash": redis.hgetall, "list": lambda key: redis.lrange(key, 0, -1)}
        keys = sorted(key for key in redis.keys(f"*{SLUG}*") if "lock" not in key and not key.endswith("rejections"))
        return {key: reads.get(redis.type(key), redis.type)(key) for key in keys}

    def changed(self, before: dict, after: dict) -> list[str]:
        keys = sorted(key for key in {*before, *after} if before.get(key) != after.get(key))
        return [key.split(f":{SLUG}:", 1)[-1] for key in keys]

    def leaked(self, *documents: object) -> list[str]:
        text = json.dumps(documents, sort_keys=True)
        return [value for value in (brain_token(), operator_credential(), self.stuck_token) if value in text]

    def masked(self, document: object) -> object:
        text = json.dumps(document, sort_keys=True)
        for label, agent in (("<stuck>", self.stuck), ("<healthy>", self.healthy)):
            text = text.replace(agent.execution_id, label).replace(agent.name, f"{label}-name")
        text = text.replace(str(self.brain.server.server_port), "<brain-port>")
        return json.loads(text)


def _archive(report: dict) -> dict[str, dict]:
    return {entry["execution_id"]: entry for entry in report["pipeline"]["archive"]}


def _refusal(action) -> str:
    try:
        action()
    except health.HealthRefused as error:
        return error.reason
    return "accepted"


def _story() -> tuple[dict, list[bool]]:
    world = World()
    with world.clocked():
        ids = world.run()
        live_status, live = world.get(health.LIVE_PATH)
        world.server.connected = False
        shallow_without_redis = world.get(health.LIVE_PATH)[0]
        world.server.connected = True
        brain_before = world.brain.requests
        status, report = world.get(health.DIAGNOSE_PATH, operator_credential())
        worker_status, worker = world.get(health.DIAGNOSE_PATH, world.healthy_token)
        archive = _archive(report)
        stuck, healthy = archive[ids["stuck"]], archive[ids["healthy"]]
        observed = {
            "live": [live_status, live["probe"], live["status"]],
            "shallow_without_redis": shallow_without_redis,
            "shallow_touched_brain": brain_before != 0,
            "diagnose": [status, report["probe"], report["status"], report["scope"]],
            "process": {name: value for name, value in report["process"].items() if name != "pid"},
            "dependencies": {name: dep["state"] for name, dep in report["dependencies"].items()},
            "brain_reason": report["dependencies"]["brain"]["reason"],
            "redis_last_ok": report["dependencies"]["redis"]["last_ok_at_ms"] - world.started_ms,
            "stuck": world.masked(stuck),
            "healthy": world.masked(healthy),
            "remediation": world.masked(report["remediation"]),
            "pipeline_stage_health": health.pipeline_stage_health(report),
            "worker": [worker_status, worker["scope"], sorted(world.masked(_archive(worker)))],
            "worker_has_transition_export": "transition_export" in worker["pipeline"],
            "brain_bearer_sent": world.brain.bearers[-1] == f"Bearer {brain_token()}",
            "leaked": world.leaked(live, report, worker),
        }
        elapsed = world.clock[0] - world.started_ms
    checks = [
        observed["live"] == [200, "shallow", "live"],
        observed["shallow_without_redis"] == 200,
        observed["shallow_touched_brain"] is False,
        observed["diagnose"] == [200, "deep", "degraded", "swarm"],
        observed["process"]["status"] == "live",
        observed["process"]["controller_lease"] == "held",
        observed["dependencies"] == {"redis": "ok", "brain": "auth_failed"},
        observed["brain_reason"] == "brain_http_401",
        observed["redis_last_ok"] == elapsed,
        observed["stuck"]["process"] == "live",
        observed["stuck"]["state"] == "stalled",
        observed["stuck"]["watermark"] == FX["stuck"]["watermark"],
        observed["stuck"]["advanced_at_ms"] == FX["clock_ms"],
        observed["healthy"]["state"] == "ok",
        observed["healthy"]["watermark"] == FX["healthy"]["watermark_step"] * FX["healthy"]["beats"],
        [hint["class"] for hint in observed["remediation"]] == ["credential_rejected", "pipeline_stalled"],
        all(hint["automatic"] is False for hint in observed["remediation"]),
        observed["pipeline_stage_health"]["archive"] == {"ok": 1, "stalled": 1},
        observed["worker"] == [200, "execution", ["<healthy>"]],
        observed["worker_has_transition_export"] is False,
        observed["brain_bearer_sent"],
        observed["leaked"] == [],
    ]
    return observed, checks


def _positive() -> tuple[dict, bool]:
    first, first_checks = _story()
    second, second_checks = _story()
    observed = {"first": first, "second": second, "independent_runs_match": first == second}
    return observed, all(first_checks) and all(second_checks) and first == second


def _rejection() -> tuple[dict, bool]:
    world = World()
    with world.clocked():
        ids = world.run()
        live = world.get(health.LIVE_PATH)[1]
        report = world.get(health.DIAGNOSE_PATH, operator_credential())[1]
        before = world.protected()
        forged = copy.deepcopy(report)
        for entry in forged["pipeline"]["archive"]:
            entry["state"] = "ok"
        refusals = {
            "shallow_resume": _refusal(lambda: world.health.qualify(live, "native_resume", ids["healthy"])),
            "shallow_backup": _refusal(lambda: world.health.qualify(live, "backup")),
            "stalled_resume": _refusal(lambda: world.health.qualify(report, "native_resume", ids["stuck"])),
            "stalled_backup": _refusal(lambda: world.health.qualify(report, "backup")),
            "forged_resume": _refusal(lambda: world.health.qualify(forged, "native_resume", ids["stuck"])),
            "unknown_purpose": _refusal(lambda: world.health.qualify(report, "restore")),
        }
        body = {"purpose": "native_resume", "execution_id": ids["healthy"], "report": live}
        http = {
            "anonymous_diagnose": world.get(health.DIAGNOSE_PATH),
            "wrong_operator": world.get(health.DIAGNOSE_PATH, "not-the-credential"),
            "worker_qualify": world.post(health.QUALIFY_PATH, world.healthy_token, body),
            "operator_shallow_qualify": world.post(health.QUALIFY_PATH, operator_credential(), body),
        }
        world.advance(FX["limits"]["report_ms"] + 1)
        refusals["stale_resume"] = _refusal(lambda: world.health.qualify(report, "native_resume", ids["healthy"]))
        after = world.protected()
        fresh = world.get(health.DIAGNOSE_PATH, operator_credential())[1]
        accepted = world.health.qualify(fresh, "native_resume", ids["healthy"])
        observed = {
            "refusals": refusals,
            "http": {
                name: [status, reply.get("error_class"), reply.get("reason", "")]
                for name, (status, reply) in http.items()
            },
            "protected_changed": world.changed(before, after),
            "fresh_resume": world.masked({name: value for name, value in accepted.items() if name != "report_id"}),
            "leaked": world.leaked(refusals, http, fresh, accepted),
        }
    checks = [
        refusals
        == {
            "shallow_resume": "shallow_report",
            "shallow_backup": "shallow_report",
            "stalled_resume": "pipeline_not_ok",
            "stalled_backup": "pipeline_not_ok",
            "forged_resume": "unrecognized_report",
            "unknown_purpose": "unsupported_purpose",
            "stale_resume": "stale_report",
        },
        observed["http"]["anonymous_diagnose"] == [401, "unauthenticated", ""],
        observed["http"]["wrong_operator"][0] == 401,
        observed["http"]["worker_qualify"] == [403, "forbidden_scope", ""],
        observed["http"]["operator_shallow_qualify"] == [409, "not_qualified", "shallow_report"],
        set(observed["protected_changed"]) <= LEASE,
        observed["fresh_resume"]["qualified"] is True,
        observed["fresh_resume"]["execution_id"] == "<healthy>",
        observed["leaked"] == [],
    ]
    return observed, all(checks)


def _recovery() -> tuple[dict, bool]:
    world = World()
    with world.clocked():
        ids = world.run()
        operator = operator_credential()
        healthy_report = world.get(health.DIAGNOSE_PATH, operator)[1]
        execution_state = world.protected()
        world.server.connected = False
        world.clock[0] += 1000
        during_status, during = world.get(health.DIAGNOSE_PATH, operator)
        worker_during = world.get(health.DIAGNOSE_PATH, world.healthy_token)[0]
        live_during = world.get(health.LIVE_PATH)[0]
        world.server.connected = True
        after_redis = world.get(health.DIAGNOSE_PATH, operator)[1]
        world.brain.rejecting = False
        world.advance(FX["limits"]["brain_interval_ms"])
        after_brain = world.get(health.DIAGNOSE_PATH, operator)[1]
        state_after = world.protected()
        final = FX["healthy"]["watermark_step"] * FX["healthy"]["beats"]
        replay_status, replay = world.beat(world.healthy, world.healthy_token, FX["healthy"]["beats"], final)
        replay_wrote_nothing = world.protected() == state_after
        stale_status, _ = world.beat(world.stuck, world.stuck_token, 1, 0)
        stuck_after_stale = _archive(world.get(health.DIAGNOSE_PATH, operator)[1])[ids["stuck"]]
        with mock.patch.dict("os.environ", {health.DEEP_ENV: "off"}):
            switched = health.HealthAPI(world.health, world.grants, deep=health.deep_enabled())
            rollback = {
                "live": switched.route("GET", health.LIVE_PATH, "None", None)[0],
                "diagnose": switched.route("GET", health.DIAGNOSE_PATH, f"Bearer {operator}", None)[0],
            }
        during_archive = _archive(during)
        observed = {
            "during": {
                "status": [during_status, during["status"]],
                "dependencies": {name: dep["state"] for name, dep in during["dependencies"].items()},
                "redis_last_ok_kept": during["dependencies"]["redis"]["last_ok_at_ms"]
                == healthy_report["dependencies"]["redis"]["last_ok_at_ms"],
                "redis_reason": during["dependencies"]["redis"]["reason"],
                "stuck": world.masked(during_archive[ids["stuck"]]),
                "healthy": world.masked(during_archive[ids["healthy"]]),
                "worker": worker_during,
                "live": live_during,
                "remediation": [hint["class"] for hint in during["remediation"]],
            },
            "after_redis": {
                "dependencies": {name: dep["state"] for name, dep in after_redis["dependencies"].items()},
                "archive": {k: v["state"] for k, v in world.masked(_archive(after_redis)).items()},
            },
            "after_brain": {
                "dependencies": {name: dep["state"] for name, dep in after_brain["dependencies"].items()},
                "remediation": [hint["class"] for hint in after_brain["remediation"]],
            },
            "execution_state_changed": world.changed(execution_state, state_after),
            "replay": [replay_status, replay["archive_watermark"], replay["renewal_sequence"], replay_wrote_nothing],
            "stale": stale_status,
            "stuck_after_stale": [stuck_after_stale["watermark"], stuck_after_stale["state"]],
            "rollback": rollback,
            "leaked": world.leaked(during, after_redis, after_brain),
        }
    checks = [
        observed["during"]["status"] == [200, "degraded"],
        observed["during"]["dependencies"] == {"redis": "unavailable", "brain": "auth_failed"},
        observed["during"]["redis_last_ok_kept"],
        observed["during"]["stuck"]["state"] == "unknown",
        observed["during"]["stuck"]["last_observed_state"] == "stalled",
        observed["during"]["stuck"]["watermark"] == FX["stuck"]["watermark"],
        observed["during"]["healthy"]["state"] == "unknown",
        observed["during"]["healthy"]["last_observed_state"] == "ok",
        observed["during"]["worker"] == 503,
        observed["during"]["live"] == 200,
        observed["during"]["remediation"] == ["dependency_unavailable", "credential_rejected"],
        observed["after_redis"]["dependencies"] == {"redis": "ok", "brain": "auth_failed"},
        observed["after_redis"]["archive"] == {"<stuck>": "stalled", "<healthy>": "ok"},
        observed["after_brain"]["dependencies"] == {"redis": "ok", "brain": "ok"},
        observed["after_brain"]["remediation"] == ["pipeline_stalled"],
        set(observed["execution_state_changed"]) <= LEASE,
        observed["replay"]
        == [200, FX["healthy"]["watermark_step"] * FX["healthy"]["beats"], FX["healthy"]["beats"], True],
        observed["stale"] == 409,
        observed["stuck_after_stale"] == [FX["stuck"]["watermark"], "stalled"],
        observed["rollback"] == {"live": 200, "diagnose": 503},
        observed["leaked"] == [],
    ]
    return observed, all(checks)


def run_case(case: str) -> dict:
    observed, passed = {"a": _positive, "b": _rejection, "c": _recovery}[case]()
    return {
        "case": f"T-SV2-OBS-04-{case.upper()}",
        "state": "passed" if passed else "failed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {name: hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() for name in INPUTS},
        "observed": observed,
    }
