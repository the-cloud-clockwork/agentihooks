"""The approved launch accounts are the key names of Secret swarm-claude-accounts, which external secrets syncs from
the source of swarm-claude-creds with every value replaced, so the controller never holds a provider credential."""

from collections.abc import Callable, Collection, Mapping

from scripts import session_bands
from scripts.swarm.capacity import Account
from scripts.swarm_v2.kubernetes.client import ApiRefused, PodApi
from scripts.swarm_v2.kubernetes.spec import KEY
from scripts.swarm_v2.quota import UNKNOWN, QuotaObservations

INDEX = "swarm-claude-accounts"
OPEN, CLOSED = "OPEN", "CLOSED"


class SecretKeys:
    def __init__(self, api: PodApi) -> None:
        self.api = api

    def __call__(self) -> frozenset[str]:
        try:
            secret = self.api.read("secrets", INDEX)
        except (ApiRefused, ConnectionError, TimeoutError) as error:
            print(
                f"controller: Secret {INDEX} is unreadable ({type(error).__name__}); no account is approved",
                flush=True,
            )
            return frozenset()
        data = (secret or {}).get("data") or {}
        return frozenset(key for key in data if KEY.fullmatch(key))


class AccountRoute:
    """An unobserved key keeps the launch cap; an observed key's quota band can only lower it."""

    def __init__(
        self,
        keys: Callable[[], Collection[str]],
        quota: QuotaObservations,
        held: Callable[[], Mapping[str, int]],
        harness: str,
    ) -> None:
        self.keys, self.quota, self.held, self.harness = keys, quota, held, harness

    def _seat(self, key: str, cap: int, held: Mapping[str, int]) -> session_bands.Seat:
        sessions = held.get(key, 0)
        reading = self.quota.reading(key, self.harness)
        if reading.state == UNKNOWN:
            return session_bands.Seat(self.harness, key, cap, sessions)
        resets = self.quota.latest(key, self.harness).week_reset
        spend = session_bands.spend_by(reading.five_left, resets)
        return session_bands.Seat(self.harness, key, min(cap, self.quota.cap(key, self.harness)), sessions, spend)

    def choose(self, cap: int) -> str | None:
        held = self.held()
        seat = session_bands.pick(self._seat(key, cap, held) for key in sorted(self.keys()))
        return None if seat is None else seat.account


class FleetAccounts:
    """The deployed controller's quota capacity rows, read like ``capacity.accounts`` but from the approved keys, the
    fleet quota observations and the fleet reservations, never from local tokens or the local process table."""

    def __init__(self, route: AccountRoute, cap: int) -> None:
        self.route, self.cap = route, cap

    def __call__(self, environ: Mapping[str, str], now: float, refresh: bool = True) -> list[Account]:
        held = self.route.held()
        return [self._row(key, held.get(key, 0), now) for key in sorted(self.route.keys())]

    def _row(self, key: str, sessions: int, now: float) -> Account:
        quota, harness = self.route.quota, self.route.harness
        reading = quota.reading(key, harness)
        if reading.state == UNKNOWN:
            return Account(harness, key, OPEN, sessions, None, None, self.cap)
        cap = min(self.cap, quota.cap(key, harness))
        latest = quota.latest(key, harness)
        return Account(
            harness,
            key,
            OPEN if cap else CLOSED,
            sessions,
            reading.five_left,
            reading.week_left,
            cap,
            session_bands.upcoming(latest.week_reset, now),
            five_resets_at=session_bands.upcoming(latest.five_reset, now),
        )
