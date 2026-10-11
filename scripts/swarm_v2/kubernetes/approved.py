"""The approved launch accounts are the key names of the shared account Secret; its values are dropped unread."""

from collections.abc import Callable, Collection, Mapping

from scripts import session_bands
from scripts.swarm_v2.kubernetes.client import ApiRefused, PodApi
from scripts.swarm_v2.kubernetes.spec import ACCOUNTS_OBJECT
from scripts.swarm_v2.quota import UNKNOWN, QuotaObservations

HARNESS = "claude"


class SecretKeys:
    def __init__(self, api: PodApi, name: str = ACCOUNTS_OBJECT) -> None:
        self.api, self.name = api, name

    def __call__(self) -> frozenset[str]:
        try:
            secret = self.api.read("secrets", self.name)
        except (ApiRefused, ConnectionError, TimeoutError) as error:
            print(
                f"controller: Secret {self.name} is unreadable ({type(error).__name__}); no account is approved",
                flush=True,
            )
            return frozenset()
        return frozenset((secret or {}).get("data") or {})


class AccountRoute:
    """An unobserved key keeps the launch cap; an observed key's quota band can only lower it."""

    def __init__(
        self, keys: Callable[[], Collection[str]], quota: QuotaObservations, held: Callable[[], Mapping[str, int]]
    ) -> None:
        self.keys, self.quota, self.held = keys, quota, held

    def _seat(self, key: str, cap: int, held: Mapping[str, int]) -> session_bands.Seat:
        sessions = held.get(key, 0)
        reading = self.quota.reading(key, HARNESS)
        if reading.state == UNKNOWN:
            return session_bands.Seat(HARNESS, key, cap, sessions)
        resets = self.quota.latest(key, HARNESS).week_reset
        spend = session_bands.spend_by(reading.five_left, resets)
        return session_bands.Seat(HARNESS, key, min(cap, self.quota.cap(key, HARNESS)), sessions, spend)

    def choose(self, cap: int) -> str | None:
        held = self.held()
        seat = session_bands.pick(self._seat(key, cap, held) for key in sorted(self.keys()))
        return None if seat is None else seat.account
