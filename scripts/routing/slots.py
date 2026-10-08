from collections.abc import Mapping
from dataclasses import KW_ONLY, dataclass
from typing import Literal, Protocol

SUBSCRIPTION = "subscription"
INTERACTIVE = "interactive"
API = "api"

Kind = Literal["subscription", "interactive", "api"]


@dataclass(frozen=True)
class Slot:
    harness: str
    account: str
    cap: int
    sessions: int
    spend_before: float | None = None
    _: KW_ONLY
    kind: Kind = SUBSCRIPTION
    weight: float | None = None

    @property
    def free(self) -> int:
        return max(0, self.cap - self.sessions)


class SlotSource(Protocol):
    def slots(self, environ: Mapping[str, str], now: float) -> list[Slot]: ...

    def child_env(self, slot: Slot, environ: Mapping[str, str]) -> dict[str, str]: ...
