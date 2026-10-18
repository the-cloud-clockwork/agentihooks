from dataclasses import dataclass

from scripts.gates import log, modes
from scripts.gates.base import Who
from scripts.swarm.store import RedisStore


@dataclass(frozen=True)
class Enforcement:
    name: str
    default_mode: str = "observe"

    def allows(self, store: RedisStore, who: Who, action: str, reason: str, now_ms: int | None = None) -> bool:
        chosen = modes.configured(self, store.config(who.swarm).gates)
        if chosen == "off":
            return False
        kind = "deny" if chosen == "enforce" else "observe"
        log.append(who.swarm, log.Row.of(self.name, kind, who, action, reason, now_ms))
        return chosen == "enforce"


STALL_WATCH = Enforcement("stall")
ENFORCEMENTS = {STALL_WATCH.name: STALL_WATCH}
