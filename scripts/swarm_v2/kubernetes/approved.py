import os
from collections.abc import Callable, Collection, Mapping

from scripts import session_bands
from scripts.account_names import CODEX_TOKEN_PREFIX, codex_token_env
from scripts.swarm import quota_handoff
from scripts.swarm.capacity import Account, offered
from scripts.swarm_v2.kubernetes.client import ApiRefused, PodApi
from scripts.swarm_v2.kubernetes.spec import KEY
from scripts.swarm_v2.quota import UNKNOWN, QuotaObservations

INDEX = "swarm-claude-accounts"
OPEN, CLOSED = "OPEN", "CLOSED"


class SecretKeys:
    def __init__(self, api: PodApi, harness: str = "claude") -> None:
        self.api, self.harness = api, harness
        self.index = {"claude": INDEX, "codex": "swarm-codex-accounts"}[harness]

    def __call__(self) -> frozenset[str]:
        try:
            secret = self.api.read("secrets", self.index)
        except (ApiRefused, ConnectionError, TimeoutError) as error:
            print(
                f"controller: Secret {self.index} is unreadable ({type(error).__name__}); no account is approved",
                flush=True,
            )
            return frozenset()
        data = (secret or {}).get("data") or {}
        if self.harness == "codex":
            return frozenset(
                account
                for key in data
                if key.startswith(CODEX_TOKEN_PREFIX)
                and KEY.fullmatch(account := key[len(CODEX_TOKEN_PREFIX) :].replace("_", "-"))
                and codex_token_env(account) == key
            )
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

    def _row(self, key: str, cap: int, sessions: int, now: float) -> Account:
        reading = self.quota.reading(key, self.harness)
        if reading.state == UNKNOWN:
            return Account(self.harness, key, OPEN, sessions, None, None, cap)
        banded = min(cap, self.quota.cap(key, self.harness))
        latest = self.quota.latest(key, self.harness)
        return Account(
            self.harness,
            key,
            OPEN if banded else CLOSED,
            sessions,
            reading.five_left,
            reading.week_left,
            banded,
            session_bands.upcoming(latest.week_reset, now),
            five_resets_at=session_bands.upcoming(latest.five_reset, now),
        )

    def rows(self, cap: int, now: float) -> list[Account]:
        held = self.held()
        return [self._row(key, cap, held.get(key, 0), now) for key in sorted(self.keys())]

    def seats(self, cap: int, predecessor: tuple[str, str] | None = None) -> list[session_bands.Seat]:
        rows = self.rows(cap, self.quota.clock() / 1000)
        if predecessor is not None:
            thresholds = quota_handoff.Thresholds.from_env(dict(os.environ))
            rows = [row for row in rows if not quota_handoff.exclusion(row, thresholds, predecessor)]
        return offered(rows)

    def choose(self, cap: int, planned: str | None = None, predecessor: tuple[str, str] | None = None) -> str | None:
        """A planned account with a free slot is kept; a quota successor never lands on the account handing off or
        on one the handoff thresholds exclude, as the capacity plan for it was made."""
        seats = self.seats(cap, predecessor)
        if any(seat.account == planned and seat.free for seat in seats):
            return planned
        seat = session_bands.pick(seats)
        return None if seat is None else seat.account


class FleetAccounts:
    """The deployed controller's quota capacity rows, read like ``capacity.accounts`` but from the approved keys, the
    fleet quota observations and the fleet reservations, never from local tokens or the local process table."""

    def __init__(self, route: AccountRoute, cap: int) -> None:
        self.route, self.cap = route, cap

    def __call__(self, environ: Mapping[str, str], now: float, refresh: bool) -> list[Account]:
        return self.route.rows(self.cap, now)
