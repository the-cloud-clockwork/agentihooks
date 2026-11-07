import json
import os
import signal
from contextlib import suppress
from hashlib import sha256
from pathlib import Path
from threading import Thread
from time import monotonic, sleep

from hooks.context.account_sessions import agent_pid
from scripts.events.config import BusConfig
from scripts.events.factory import open_bus

VARIABLE = "AGENTIHOOKS_CLAIM_FENCE"
ATTEMPTS = 3
DELAY = 0.2
BOUND = 1.0
CHECK_FAILED = "Claim ownership could not be verified; agent actions are blocked."
REVOKED = "Claim ownership changed; this agent is ending."


def terminate(pid: int) -> None:
    if pid > 1 and pid != os.getpid():
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def owned(raw: str) -> bool | None:
    """Return whether the pinned claim is still held, or None when the bus read fails."""
    try:
        claim = json.loads(raw)
        bus = open_bus(BusConfig.from_environ())
        try:
            entry = bus.state("spoke-work").get(claim["key"])
            current = json.loads(entry.value) if entry else {}
            revision = current.get("claim_revision", entry.revision if entry else None)
            return current.get("state") in ("launching", "started") and revision == claim["revision"]
        finally:
            bus.close()
    except Exception:
        return None


def read(raw: str, deadline: float, answers: list[bool]) -> None:
    """Retry the claim read until it answers, the attempts run out or the deadline passes."""
    for attempt in range(ATTEMPTS):
        if attempt:
            sleep(DELAY)
        if monotonic() >= deadline:
            return
        held = owned(raw)
        if held is not None:
            answers.append(held)
            return


def bounded(raw: str) -> bool | None:
    """Return the claim read's answer within BOUND seconds, or None when it fails or runs late."""
    answers: list[bool] = []
    worker = Thread(target=read, args=(raw, monotonic() + BOUND, answers), daemon=True)
    worker.start()
    worker.join(BOUND)
    return answers[0] if answers else None


def marker(raw: str) -> Path:
    home = Path(os.environ.get("AGENTIHOOKS_HOME") or Path.home() / ".agentihooks")
    return home / "claim-fence" / sha256(raw.encode()).hexdigest()


def cached(raw: str) -> bool:
    """Return whether this claim generation was already seen held."""
    return marker(raw).exists()


def remember(raw: str) -> None:
    """Record that this claim generation was seen held."""
    path = marker(raw)
    if path.exists():
        return
    with suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()


def check(payload: dict) -> str | None:
    raw = os.environ.get(VARIABLE)
    if not raw or payload.get("hook_event_name") == "SessionEnd":
        return None
    held = bounded(raw)
    if held:
        remember(raw)
        return None
    if held is None:
        return None if cached(raw) else CHECK_FAILED
    terminate(agent_pid())
    return REVOKED


def enforce(payload: dict) -> None:
    from hooks.hook_manager import BlockAction

    message = check(payload)
    if message:
        raise BlockAction(message)
