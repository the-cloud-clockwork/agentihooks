import hashlib
import json
import time
from dataclasses import replace

from .envelope import Envelope

DEAD_STREAM = "AGENTIHOOKS_DEAD"
DEAD_SUBJECT = "dead.>"


def dead_letter(event: Envelope, source: str, attempts: int) -> Envelope:
    identity = json.dumps([source, event.id]).encode()
    return replace(
        event,
        id=hashlib.sha256(identity).hexdigest(),
        type="dead_lettered",
        time=time.time_ns() // 1_000_000,
        cause=event.id,
        data={"event": event.to_dict(), "source": source, "attempts": attempts},
    )
