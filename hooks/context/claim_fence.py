import json
import os
import signal

from hooks.context.account_sessions import agent_pid
from scripts.events.config import BusConfig
from scripts.events.factory import open_bus

VARIABLE = "AGENTIHOOKS_CLAIM_FENCE"
CHECK_FAILED = "Claim ownership could not be verified; agent actions are blocked."
REVOKED = "Claim ownership changed; this agent is ending."


def terminate(pid: int) -> None:
    if pid > 1 and pid != os.getpid():
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def check(payload: dict) -> str | None:
    raw = os.environ.get(VARIABLE)
    if not raw or payload.get("hook_event_name") == "SessionEnd":
        return None
    try:
        claim = json.loads(raw)
        bus = open_bus(BusConfig.from_environ())
        try:
            entry = bus.state("spoke-work").get(claim["key"])
            current = json.loads(entry.value) if entry else {}
            revision = current.get("claim_revision", entry.revision if entry else None)
            owned = current.get("state") in ("launching", "started") and revision == claim["revision"]
        finally:
            bus.close()
    except Exception:
        return CHECK_FAILED
    if owned:
        return None
    terminate(agent_pid())
    return REVOKED


def enforce(payload: dict) -> None:
    from hooks.hook_manager import BlockAction

    message = check(payload)
    if message:
        raise BlockAction(message)
