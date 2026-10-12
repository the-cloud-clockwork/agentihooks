import hashlib
import json
from pathlib import Path

import fakeredis
import redis

from scripts.swarm.store import RedisStore
from scripts.swarm_v2 import fleet_canary
from scripts.swarm_v2 import fleet_dashboard as fleet

FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2"
DEFINITION = Path(__file__).parents[1] / "observability" / "swarm-v2.json"
INPUTS = ("fleet-dashboard.json", "fleet-dashboard-second.json")
RECORDED = ("scaling-canary-sv2-cap-02.json", "fleet-canary-overlay.json")
EVIDENCE_CLASS = (
    "mixed: the two live runs of the recorded SV2-CAP-02 scaling canary on the Anton anton-k3s-swarm group, joined "
    "with synthetic quota wait, stale checkpoint and denied cross brain query readings, plus two independent "
    "synthetic traces, a fakeredis incident store behind a server that can be disconnected and an in process "
    "notification sink that can fail; no live controller, deployed brain or alert backend was used"
)
ON: dict[str, str] = {}
OFF = {fleet.ALERTS_ENV: "off"}


def fixture() -> dict:
    first, second = (json.loads((FIXTURES / name).read_text()) for name in INPUTS)
    return {**first, "traces": {**first["traces"], **second["traces"]}}


FX = fixture()
FOUND = fleet.definition(DEFINITION)
FOUND_PANELS = [panel["id"] for panel in FOUND["panels"]]
OVERLAY_DENIALS = json.loads((FIXTURES / RECORDED[1]).read_text())["brain"]["denials"]
TITLES = sorted(
    {d["session_title"] for t in FX["traces"].values() for s in t for d in s["brain"]["denials"]}
    | {d["session_title"] for d in OVERLAY_DENIALS}
)


class World:
    def __init__(self, slug: str) -> None:
        self.server = fakeredis.FakeServer()
        self.store = RedisStore(fakeredis.FakeRedis(server=self.server, decode_responses=True))
        self.slug = slug
        self.notices: list[dict] = []
        self.failing = False

    def incidents(self) -> fleet.IncidentStore:
        return fleet.IncidentStore(self.store, self.slug)

    def sink(self, notice: dict) -> None:
        if self.failing:
            raise ConnectionError("notification backend unavailable")
        self.notices.append(notice)

    def snapshot(self) -> dict:
        return {key: self.store.redis.dump(key) for key in sorted(self.store.redis.keys("*"))}


def leaked(*outputs: object) -> list[str]:
    text = json.dumps(outputs, sort_keys=True, default=str)
    return [title for title in TITLES if title in text]


def first_fired(world: World, trace: list[dict]) -> tuple[dict, list[dict]]:
    fired, records = {}, []
    for sample in trace:
        evaluated = fleet.evaluate(sample, FOUND, ON)
        records.append(world.incidents().record(evaluated))
        world.incidents().deliver(world.sink, ON)
        for alert in evaluated["alerts"]:
            fired.setdefault(f"{alert['rule']}:{alert['subject']}", sample["seq"])
    return fired, records


def labels(view: dict) -> list[list[str]]:
    return [
        [row["series"][key]["label"] for key in ("configured_maximum", "observed_nodes", "active_tasks")]
        for row in view["panels"]["capacity"]
    ]


def _run_trace(name: str) -> dict:
    world = World(name)
    trace = FX["traces"][name]
    fired, records = first_fired(world, trace)
    last = trace[-1]
    view = fleet.dashboard(last, fleet.viewer(FX["grants"]["swarm"]), FOUND)
    panels = view["panels"]
    evaluated = fleet.evaluate(last, FOUND, ON)
    return {
        "first_fired_seq": fired,
        "opened": sum(len(r["opened"]) for r in records),
        "notices": sorted(n["rule"] for n in world.notices),
        "capacity": [
            [row["pool"], *[row["series"][key]["value"] for key in row["series"]]] for row in panels["capacity"]
        ],
        "capacity_labels_distinct": all(len(set(row)) == 3 for row in labels(view)),
        "attempts": [panels["attempts"]["active"]["value"], panels["attempts"]["pending"]["value"]],
        "quota_wait": [panels["quota_wait"][k]["value"] for k in ("waiting_accounts", "longest_wait_ms")],
        "checkpoint_age": [panels["checkpoint_age"][k]["value"] for k in ("stale", "oldest_age_ms")],
        "archive_lag": [panels["archive_lag"][k]["value"] for k in ("lagging", "worst_lag_ms")],
        "connectivity": {k: panels["connectivity"][k]["value"] for k in ("terminal", "authority")},
        "knowledge_stale": panels["knowledge"]["stale"],
        "alert_actionability_review": fleet.alert_actionability_review(evaluated, FOUND),
        "leaked": leaked(world.snapshot(), world.notices, evaluated),
    }


def _run_recorded() -> dict:
    record, overlay = (json.loads((FIXTURES / name).read_text()) for name in RECORDED)
    at_maximum = json.loads(json.dumps(record))
    for run in at_maximum["runs"]:
        run["classification"]["group"]["max"] = 0
    observed = {}
    for name, trace in fleet_canary.record_samples(record, overlay).items():
        world = World(name)
        fired, _ = first_fired(world, trace)
        panels = fleet.dashboard(trace[-1], fleet.viewer(FX["grants"]["swarm"]), FOUND)["panels"]
        capacity = panels["capacity"][0]
        control, _ = first_fired(World(name), fleet_canary.record_samples(at_maximum, overlay)[name])
        observed[name] = {
            "pending_ms": trace[-1]["at_ms"] - trace[0]["at_ms"],
            "first_fired_seq": fired,
            "at_maximum_first_fired_seq": control,
            "capacity": [capacity["pool"], *[capacity["series"][key]["value"] for key in capacity["series"]]],
            "checkpoint_stale": panels["checkpoint_age"]["stale"]["value"],
            "quota_waiting": panels["quota_wait"]["waiting_accounts"]["value"],
            "leaked": leaked(world.snapshot(), world.notices, panels),
        }
    return observed


def _positive() -> tuple[dict, bool]:
    observed = {"first": _run_trace("first"), "second": _run_trace("second"), "recorded": _run_recorded()}
    recorded = observed["recorded"].values()
    first, second = observed["first"], observed["second"]
    checks = [
        first["first_fired_seq"]
        == {"impossible_pending:exe-huge-1": 3, "cross_scope_denial:deny-1": 4, "protected_idle_pod:exe-idle-1": 5},
        "impossible_pending:exe-scale-1" not in first["first_fired_seq"],
        first["opened"] == 3
        and first["notices"] == sorted(["impossible_pending", "cross_scope_denial", "protected_idle_pod"]),
        first["capacity"][0] == ["on-demand", 20, 1, 1, 0],
        first["capacity_labels_distinct"] and second["capacity_labels_distinct"],
        first["checkpoint_age"][0] == 1,
        first["quota_wait"][0] == 1,
        first["connectivity"]["terminal"].get("disconnected") == 1 and first["connectivity"]["authority"]["held"] == 2,
        second["first_fired_seq"]["impossible_pending:exe-full-2"] == 3,
        not any(key.startswith("protected_idle_pod") for key in second["first_fired_seq"]),
        second["alert_actionability_review"]["suppressed"]["cross_scope_denial"] == 1,
        second["knowledge_stale"],
        all(
            o["alert_actionability_review"]["actionable"] == o["alert_actionability_review"]["alerts"]
            for o in (first, second)
        ),
        first["leaked"] == [] and second["leaked"] == [],
        len(recorded) == 2,
        all(run["pending_ms"] > 120000 for run in recorded),
        all(list(run["first_fired_seq"]) == ["cross_scope_denial:deny-anton-1"] for run in recorded),
        all(
            run["at_maximum_first_fired_seq"].get(f"impossible_pending:{name}") == 5
            for name, run in observed["recorded"].items()
        ),
        all(run["capacity"] == ["anton-k3s-swarm", 4, 1, 1, 0] for run in recorded),
        all(run["checkpoint_stale"] == 1 and run["quota_waiting"] == 1 for run in recorded),
        all(run["leaked"] == [] for run in recorded),
    ]
    return observed, all(checks)


def _rejection() -> tuple[dict, bool]:
    world = World("rejection")
    trace = FX["traces"]["first"]
    first_fired(world, trace)
    before = world.snapshot()
    last = trace[-1]
    try:
        fleet.dashboard(last, fleet.viewer(FX["grants"]["labelled"]), FOUND)
        refused = ""
    except fleet.ViewRefused as error:
        refused = str(error)
    swarm = fleet.dashboard(last, fleet.viewer(FX["grants"]["swarm"]), FOUND)
    operator = fleet.dashboard(last, fleet.viewer(FX["grants"]["operator"]), FOUND)
    evaluated = fleet.evaluate(last, FOUND, ON)
    observed = {
        "labelled_grant": refused,
        "swarm_denials": [
            [row["session_title"], row["target_brain"]] for row in swarm["panels"]["knowledge"]["denials"]
        ],
        "operator_sees_title": [
            row["session_title"] in TITLES and row["target_brain"] == "personal-operator"
            for row in operator["panels"]["knowledge"]["denials"]
        ],
        "swarm_leaked": leaked(swarm),
        "alert_leaked": leaked(evaluated, world.snapshot(), world.notices),
        "store_unchanged": world.snapshot() == before,
    }
    checks = [
        observed["labelled_grant"] == "a viewer grant names its brains as a list of brain ids",
        observed["swarm_denials"] == [[fleet.WITHHELD, fleet.WITHHELD]],
        observed["operator_sees_title"] == [True],
        observed["swarm_leaked"] == [],
        observed["alert_leaked"] == [],
        observed["store_unchanged"],
    ]
    return observed, all(checks)


def _recovery() -> tuple[dict, bool]:
    world = World("recovery")
    trace = FX["traces"]["first"]
    first_fired(world, trace[:4])
    world.server.connected = False
    try:
        world.incidents().record(fleet.evaluate(trace[4], FOUND, ON))
        outage = "recorded"
    except redis.ConnectionError:
        outage = "refused"
    world.server.connected = True
    restarted = world.incidents()
    after = restarted.record(fleet.evaluate(trace[4], FOUND, ON))
    replay = restarted.record(fleet.evaluate(trace[4], FOUND, ON))
    older = restarted.record(fleet.evaluate(trace[2], FOUND, ON))
    world.failing = True
    try:
        restarted.deliver(world.sink, ON)
        failed_delivery = "delivered"
    except ConnectionError:
        failed_delivery = "raised"
    world.failing = False
    delivered = restarted.deliver(world.sink, ON)
    again = restarted.deliver(world.sink, ON)
    before_rollback = world.snapshot()
    rollback = {
        "evaluate": fleet.evaluate(trace[5], FOUND, OFF)["state"],
        "record": restarted.record(fleet.evaluate(trace[5], FOUND, OFF))["disabled"],
        "deliver": restarted.deliver(world.sink, OFF),
        "dashboard": sorted(fleet.dashboard(trace[5], fleet.viewer(FX["grants"]["swarm"]), FOUND)["panels"]),
        "store_unchanged": world.snapshot() == before_rollback,
    }
    incidents = restarted.incidents()
    ids = [n["incident_id"].split(":")[0] for n in world.notices]
    observed = {
        "outage": outage,
        "after_restore": after["opened"] == [fleet.fingerprint("protected_idle_pod", "exe-idle-1")],
        "replay_stale": replay["stale"],
        "older_stale": older["stale"],
        "last_seq": sorted({row["last_seq"] for row in incidents.values()}),
        "failed_delivery": failed_delivery,
        "delivered_after_restore": [delivered, again],
        "notices_per_incident": sorted(ids.count(fp) for fp in incidents),
        "incidents": len(incidents),
        "rollback": rollback,
    }
    checks = [
        observed["outage"] == "refused",
        observed["after_restore"],
        observed["replay_stale"] and observed["older_stale"],
        observed["last_seq"] == [5],
        observed["failed_delivery"] == "raised",
        observed["delivered_after_restore"] == [1, 0],
        observed["notices_per_incident"] == [1, 1, 1],
        rollback["evaluate"] == "disabled" and rollback["record"] and rollback["deliver"] == 0,
        rollback["dashboard"] == sorted(FOUND_PANELS),
        rollback["store_unchanged"],
    ]
    return observed, all(checks)


def run_case(case: str) -> dict:
    observed, passed = {"a": _positive, "b": _rejection, "c": _recovery}[case]()
    return {
        "case": f"T-SV2-OBS-05-{case.upper()}",
        "state": "passed" if passed else "failed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {
            name: hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() for name in INPUTS + RECORDED
        },
        "observed": observed,
    }
