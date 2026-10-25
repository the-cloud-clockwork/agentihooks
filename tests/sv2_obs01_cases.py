import copy
import hashlib
import json
from unittest import mock

from scripts.swarm import lease
from scripts.swarm.store import AgentRecord, SwarmError
from scripts.swarm_v2 import telemetry
from scripts.swarm_v2.runtime import observe
from tests import sv2_kub05_cases as kub05

FIXTURES = kub05.FIXTURES
INPUTS = ("transition-telemetry.json", *kub05.INPUTS)
SLUG = kub05.SLUG
OUTAGE = ("state", "events", "unexported", "export_failures", "last_error")
EVIDENCE_CLASS = (
    "mocked: fakeredis execution registry, transition audit, latency counters and observability record, a fake "
    "lease clock, a fixed step telemetry clock, an in process exporter that can be switched off, an in process API "
    "server whose Pods carry fixture statuses and the SV2-KUB-05 recovery world; no Kubernetes API server, node, "
    "worker Pod or telemetry backend was used"
)


def fixture() -> dict:
    return json.loads((FIXTURES / "transition-telemetry.json").read_text())


def secret() -> str:
    return "-".join(fixture()["fake_secret_parts"])


def environment() -> dict[str, str]:
    return {name: value.format(secret=secret()) for name, value in fixture()["fake_environment"].items()}


class Ticks:
    def __init__(self, step: float) -> None:
        self.step, self.now = step, 0.0

    def __call__(self) -> float:
        self.now = round(self.now + self.step, 6)
        return self.now


class Exporter:
    def __init__(self) -> None:
        self.received: list[dict] = []
        self.down = False

    def __call__(self, event: dict) -> None:
        if self.down:
            raise ConnectionError(f"collector unreachable with token={secret()}")
        self.received.append(copy.deepcopy(event))


class World(kub05.World):
    def __init__(self, exporting: bool = True) -> None:
        super().__init__()
        self.exporter = Exporter() if exporting else None
        self.ticks = Ticks(fixture()["step_seconds"])

    def controller(self, creation_enabled: bool = True):
        controller, transport = super().controller(creation_enabled)
        controller.telemetry = telemetry.Telemetry(controller.store, SLUG, self.exporter, self.ticks)
        return controller, transport

    def pending(self, seat: str = "eng-1") -> AgentRecord:
        return AgentRecord(
            self.store.next_name(SLUG, "eng"),
            "eng",
            "task",
            seat=f"{seat}@{SLUG}",
            runtime_backend="kubernetes",
            runtime_target={"pod_namespace": self.api.namespace, "pod_name": "swarm-pending"},
        )

    def beat(self, controller, attempt: AgentRecord) -> None:
        observer = observe.Observer(controller.store, "kubernetes", observe.Thresholds())
        now = self.clock[0] / 1000
        signal = observe.Signal(
            observe.Source.HEARTBEAT, observe.Reading.OK, now, attempt.execution_id, attempt.generation, "working"
        )
        controller.observe(observer, attempt, [signal], now)

    def observability(self) -> dict:
        found = telemetry.observability(self.store, SLUG)
        return {name: found[name] for name in OUTAGE}

    def disclosed(self, controller) -> str:
        keys = (telemetry.LATENCY, telemetry.OBSERVABILITY)
        stored = {name: self.store.redis.hgetall(self.store.key(SLUG, name)) for name in keys}
        exported = [] if self.exporter is None else self.exporter.received
        return json.dumps({"audit": controller.telemetry.events(), "exported": exported, **stored}, sort_keys=True)


def counts(controller) -> dict:
    histogram = controller.telemetry.execution_transition_latency_seconds()
    return {
        transition: {result: row["count"] for result, row in results.items() if row["count"]}
        for transition, results in histogram.items()
        if any(row["count"] for row in results.values())
    }


def leaked(text: str) -> list[str]:
    values = [secret(), *environment().values(), "AH_CC_TOKEN_FIXTURE"]
    return [value for value in values if value in text]


def masked(observed: dict, events: list[dict], *attempts: AgentRecord) -> dict:
    text = json.dumps(observed, sort_keys=True)
    for label, attempt in zip(("<old>", "<replacement>", "<other>", "<third>"), attempts):
        text = text.replace(attempt.execution_id, label)
    operations = list(dict.fromkeys(event["operation_id"] for event in events if "operation_id" in event))
    for number, operation in enumerate(operations, 1):
        text = text.replace(operation, f"<op-{number}>")
    return json.loads(text)


def _refused(action) -> str:
    try:
        action()
    except SwarmError as error:
        return str(error)
    return "accepted"


def _failed(action) -> str:
    try:
        action()
    except RuntimeError as error:
        return type(error).__name__
    return "accepted"


def _story(name: str) -> dict:
    world = World()
    run = world.fx[name]
    with world.clocked():
        controller, _ = world.controller()
        delayed = _refused(lambda: controller.admit(world.pending()))
        assert controller.acquire()
        old = world.launch(controller, run)
        world.beat(controller, old)
        world.beat(controller, old)
        recovery = world.recovery(controller)
        world.reconcile(recovery)
        world.ready.discard(run["node"])
        world.api.objects.pop(f"swarm-{old.execution_id}")
        world.reconcile(recovery)
        replacement = world.store.execution(SLUG, recovery.decision(old.execution_id).replacement)
        controller.execute(world.request(controller, replacement))
        events = controller.telemetry.events()
        observed = {
            "delayed": delayed,
            "history": telemetry.history(events, old.seat),
            "audited": [event["seq"] for event in events],
            "exported": [event["seq"] for event in world.exporter.received],
            "observability": world.observability(),
            "execution_transition_latency_seconds": counts(controller),
            "attempts": world.attempts(),
            "occupant": world.occupant().execution_id,
            "leaked": leaked(world.disclosed(controller)),
        }
    return masked(observed, events, old, replacement)


def _steps(history: list[dict]) -> list[list]:
    return [[event["transition"], event["result"]] for event in history]


def _positive() -> tuple[dict, bool]:
    first, second = _story("first"), _story("second")
    checks = []
    for run in (first, second):
        history = run["history"]
        fence = next(event for event in history if event["transition"] == "fence")
        decision = next(event for event in history if event["transition"] == "recovery")
        restored = next(event for event in history if "previous_execution_id" in event)
        checks += [
            run["delayed"] == "the controller lease is absent",
            _steps(history)
            == [
                ["admission", "refused"],
                ["admission", "accepted"],
                ["command", "accepted"],
                ["heartbeat", "accepted"],
                ["fence", "accepted"],
                ["admission", "accepted"],
                ["recovery", "accepted"],
                ["command", "accepted"],
            ],
            history[0]["error"] == "SwarmError: the controller lease is absent",
            (fence["execution_id"], fence["reason"], fence["expected_generation"]) == ("<old>", "node_lost", 1),
            (restored["previous_execution_id"], restored["execution_id"]) == ("<old>", "<replacement>"),
            (restored["expected_generation"], restored["observed_generation"]) == (2, 2),
            (decision["mode"], decision["replacement"], decision["observed_generation"])
            == ("resume", "<replacement>", 2),
            all(
                event["expected_generation"] == event["observed_generation"]
                for event in history[1:]
                if event["transition"] != "recovery"
            ),
            run["exported"] == run["audited"],
            run["observability"]["state"] == "ok",
            run["observability"]["unexported"] == 0,
            run["execution_transition_latency_seconds"]["heartbeat"] == {"accepted": 2},
            run["attempts"] == [["<old>", 1], ["<replacement>", 2]],
            run["occupant"] == "<replacement>",
            run["leaked"] == [],
        ]
    observed = {"first": first, "second": second, "independent_runs_match": first == second}
    return observed, all(checks) and first == second


def _rejection() -> tuple[dict, bool]:
    world = World()
    with world.clocked():
        controller, transport = world.controller()
        assert controller.acquire()
        old = world.launch(controller, world.fx["first"])
        others = [controller.admit(world.pending(seat)) for seat in ("eng-2", "eng-3")]
        before = world.protected()
        raised = {}
        templates = fixture()["failure_messages"]
        for other, (name, template) in zip(others, templates.items()):
            text = template.format(secret=secret(), environment=environment())
            with mock.patch.object(transport, "apply_operation", side_effect=RuntimeError(text)):
                raised[name] = _failed(lambda other=other: controller.execute(world.request(controller, other)))
        world.grant["allowed"] = False
        refused = _refused(lambda: controller.admit(world.pending("eng-4")))
        unchanged = world.protected() == before
        world.grant["allowed"] = True
        corrected = controller.admit(world.pending("eng-4"))
        events = controller.telemetry.events()
        observed = {
            "raised": raised,
            "refused": refused,
            "protected_state_unchanged": unchanged,
            "failed_errors": [event["error"] for event in events if event["result"] == "failed"],
            "refused_errors": [event["error"] for event in events if event["result"] == "refused"],
            "corrected": [corrected.seat, corrected.generation],
            "leaked": leaked(world.disclosed(controller)),
            "execution_transition_latency_seconds": counts(controller),
        }
    observed = masked(observed, events, old, *others, corrected)
    checks = [
        observed["raised"] == {"environment": "RuntimeError", "token": "RuntimeError"},
        observed["refused"] == "a scoped controller grant is required",
        observed["protected_state_unchanged"],
        observed["failed_errors"]
        == [
            f"RuntimeError: {telemetry.WITHHELD}",
            "RuntimeError: upstream refused Authorization: Bearer [redacted] for the fixture",
        ],
        observed["refused_errors"] == ["SwarmError: a scoped controller grant is required"],
        observed["corrected"] == [f"eng-4@{SLUG}", 1],
        observed["leaked"] == [],
        observed["execution_transition_latency_seconds"]["command"] == {"accepted": 1, "failed": 2},
    ]
    return observed, all(checks)


def _outage(world: World) -> dict:
    with world.clocked():
        controller, transport = world.controller()
        assert controller.acquire()
        world.exporter.down = True
        old = world.launch(controller, world.fx["first"])
        request = world.request(controller, old)
        replayed = controller.execute(request)
        during = world.observability()
        committed = [world.occupant().execution_id, sorted(world.api.objects)]
        world.exporter.down = False
        world.beat(controller, old)
        after = world.observability()
        exported = [event["seq"] for event in world.exporter.received]
        world.clock[0] += lease.ttl_ms() + 1
        newer, _ = world.controller()
        assert newer.acquire()
        stale = [_refused(lambda: controller.admit(world.pending())), _refused(lambda: controller.execute(request))]
        events = newer.telemetry.events()
        commands = [event.get("operation_id", "") for event in events if event["transition"] == "command"]
        observed = {
            "during_outage": during,
            "committed_during_outage": committed,
            "replayed_operation": [replayed.operation_id, replayed.phase],
            "commands": commands,
            "after_restore": after,
            "exported": exported,
            "stale": stale,
            "occupant_after_stale": world.occupant().execution_id,
            "attempts": world.attempts(),
            "leaked": leaked(world.disclosed(newer)),
        }
    return masked(observed, events, old)


def _rollback() -> dict:
    world = World(exporting=False)
    with world.clocked():
        controller, _ = world.controller()
        assert controller.acquire()
        world.launch(controller, world.fx["first"])
        return {
            "observability": world.observability(),
            "audited": [event["transition"] for event in controller.telemetry.events()],
            "execution_transition_latency_seconds": counts(controller),
        }


def _recovery() -> tuple[dict, bool]:
    observed = {**_outage(World()), "rollback": _rollback()}
    during, after = observed["during_outage"], observed["after_restore"]
    checks = [
        during["state"] == "degraded",
        during["export_failures"] >= 1,
        during["unexported"] == 3,
        during["last_error"] == "ConnectionError: collector unreachable with token=[redacted]",
        observed["committed_during_outage"][0] == "<old>",
        observed["replayed_operation"] == ["<op-1>", "applied"],
        observed["commands"] == ["<op-1>", "<op-1>", ""],
        len(observed["committed_during_outage"][1]) == 1,
        after["state"] == "ok",
        after["unexported"] == 0,
        observed["exported"] == [1, 2, 3, 4],
        all(reason != "accepted" for reason in observed["stale"]),
        observed["occupant_after_stale"] == "<old>",
        observed["attempts"] == [["<old>", 1]],
        observed["leaked"] == [],
        observed["rollback"]["observability"]["state"] == "disabled",
        observed["rollback"]["observability"]["unexported"] == 0,
        observed["rollback"]["audited"] == ["admission", "command"],
    ]
    return observed, all(checks)


def run_case(case: str) -> dict:
    observed, passed = {"a": _positive, "b": _rejection, "c": _recovery}[case]()
    return {
        "case": f"T-SV2-OBS-01-{case.upper()}",
        "state": "passed" if passed else "failed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {name: hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() for name in INPUTS},
        "observed": observed,
    }
