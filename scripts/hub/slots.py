"""Account slot admission the hub answers for hive members, so one account total spans every swarm."""

import json
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

from scripts.events.bus import Bus
from scripts.events.subjects import SLOTS
from scripts.hive import account_slots, presence

if TYPE_CHECKING:
    from scripts.hub.admission import Admission

BUCKETS = ("account-slots", "hive-slot-accounts")
QUEUE = "hub-slots"
OPERATIONS = {
    "take": lambda slots, request: slots.take(request["execution"], request["harness"], request.get("preferred", "")),
    "heartbeat": lambda slots, request: slots.heartbeat(request["report"], request["executions"]),
    "release": lambda slots, request: slots.release(request["execution"], request["harness"], request["account"]),
}


def _answer(bus: Bus, subject: str, data: bytes, clock: Callable[[], int], admitter: "Admission | None"):
    from scripts.hub import admission

    parts = subject.split(".")
    if len(parts) != 3 or parts[0] != SLOTS:
        raise ValueError(f"not a slot request subject: {subject!r}")
    request = json.loads(data)
    operation = OPERATIONS.get(request.get("op"))
    admit = request.get("op") in admission.OPERATIONS
    if operation is None and not admit:
        raise ValueError(f"unknown slot operation {request.get('op')!r}")
    if request["hive"] != parts[2]:
        refusal = (
            f"slot request names hive {request['hive']!r} but the sending credential is bound to hive {parts[2]!r}"
        )
        print(f"hub: refused a {request['op']} in swarm {parts[1]}: {refusal}", file=sys.stderr)
        raise PermissionError(refusal)
    if admit:
        return getattr(admitter, request["op"])(parts[1], request)
    return operation(account_slots.Slots(bus, request["hive"], lambda: request["report"], clock), request)


def handle(
    bus: Bus, subject: str, data: bytes, clock: Callable[[], int] = presence.now_ms, admitter: "Admission | None" = None
) -> bytes:
    try:
        answer = {"ok": True, "result": _answer(bus, subject, data, clock, admitter)}
    except KeyError as exc:
        answer = {"ok": False, "error": f"slot request is missing {exc.args[0]!r}"}
    except Exception as exc:
        answer = {"ok": False, "error": str(exc) or type(exc).__name__}
    return json.dumps(answer).encode()


def serve(bus: Bus, clock: Callable[[], int] = presence.now_ms, admitter: "Admission | None" = None) -> None:
    from scripts.hub import admission

    admitter = admitter or admission.Admission()
    for bucket in BUCKETS:
        bus.state(bucket)
    bus.serve(f"{SLOTS}.*.*", QUEUE, lambda subject, data: handle(bus, subject, data, clock, admitter))
