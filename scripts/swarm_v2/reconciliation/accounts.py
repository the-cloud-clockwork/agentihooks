"""Reconciles this swarm's fleet account rows with the execution registry, the fleet session records and the stored
runtime observations. A row is released only on classified evidence: an expired reservation, a closed session, a lost
classification of its own generation, a runtime exit event, or a handoff whose successor occupancy is confirmed. Every
risky case stays counted and is reported. No process table or process environment is read here."""

import os
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace

from scripts.swarm.store import AgentRecord, RedisStore, SwarmError
from scripts.swarm_v2.accounts import (
    ENDED,
    OCCUPIED,
    RESERVED,
    RETIRING,
    AccountCapacity,
    Slot,
    seat_holder,
    stored_accounts,
)
from scripts.swarm_v2.registry import CLOSED, Session
from scripts.swarm_v2.registry import decode as decode_session
from scripts.swarm_v2.runtime import observe
from scripts.swarm_v2.runtime.base import LOCAL

MODE = "AGENTIHOOKS_ACCOUNT_RECONCILE"
ENFORCE, OBSERVE_ONLY = "enforce", "observe"
EXIT_VALUES = {observe.Source.KUBERNETES.value: observe.ENDED, observe.Source.SUPERVISOR.value: {observe.EXITED}}
RELEASE, KEEP, HELD_BACK = "release", "keep", "observe_only"
EXPIRED, ENDED_SESSION = "expired_reservation", "ended_session"
ORPHAN_RESERVATION, ORPHAN_OCCUPANCY = "orphan_reservation", "orphan_occupancy"
HANDOFF_UNCONFIRMED, HANDOFF_CONFIRMED = "handoff_unconfirmed", "handoff_confirmed"
WAITING_QUOTA, TERMINAL_LOSS = "waiting_quota", "terminal_loss"
UNACCOUNTED, EXITED, STALE_EXIT = "unaccounted_session", "exited", "stale_exit"
DISCREPANCIES = frozenset((ORPHAN_RESERVATION, ORPHAN_OCCUPANCY, HANDOFF_UNCONFIRMED, UNACCOUNTED))

Judged = tuple[str, str, str] | None


@dataclass(frozen=True)
class Finding:
    kind: str
    action: str
    account: str = ""
    holder: str = ""
    execution_id: str = ""
    generation: int = 0
    evidence: str = ""


def _evidence(seen: observe.Classification) -> str:
    return f"{seen.state.value}/{seen.failure.value}"


def _shows_exit(seen: observe.Classification | None, source: str) -> bool:
    entry = seen.sources.get(source) if seen is not None else None
    if entry is None:
        return False
    return entry["reading"] == observe.Reading.OK.value and entry["value"] in EXIT_VALUES[source]


def exit_source(seen: observe.Classification) -> str | None:
    return next((source for source in EXIT_VALUES if _shows_exit(seen, source)), None)


def _no_grant(_token: str) -> None:
    return None


class AccountReconciler:
    """Controller-side: it holds no launch grant, so every grant operation of its capacity store is refused."""

    def __init__(
        self,
        store: RedisStore,
        slug: str,
        clock: Callable[[], int] | None = None,
        environ: Mapping[str, str] = os.environ,
    ) -> None:
        self.store, self.slug = store, slug
        self.capacity = AccountCapacity(store, slug, _no_grant, clock)
        self.mode = OBSERVE_ONLY if environ.get(MODE) == OBSERVE_ONLY else ENFORCE
        self.measured = store.key(slug, "account-occupancy-discrepancies")
        self.stale_exits = store.key(slug, "account-stale-exits")

    def _rows(self) -> dict[str, dict[str, Slot]]:
        mine, rows = f"{self.slug}/", {}
        for account in stored_accounts(self.store):
            found = {name: slot for name, slot in self.capacity.rows(account).items() if name.startswith(mine)}
            if found:
                rows[account] = found
        return rows

    def holds(self, execution_id: str, generation: int) -> bool:
        return any(
            (slot.execution_id, slot.generation) == (execution_id, generation)
            for found in self._rows().values()
            for slot in found.values()
        )

    def _seat(self, name: str) -> str:
        return seat_holder(name).removeprefix(f"{self.slug}/")

    def _observed(self, slot: Slot) -> observe.Classification | None:
        seen = observe.stored(self.store, self.slug, slot.execution_id)
        return seen if seen is not None and seen.generation == slot.generation else None

    @staticmethod
    def _occupies(rows: dict[str, dict[str, Slot]], agent: AgentRecord) -> bool:
        return any(
            slot.state == OCCUPIED
            and RETIRING not in name
            and (slot.execution_id, slot.generation) == (agent.execution_id, agent.generation)
            for found in rows.values()
            for name, slot in found.items()
        )

    def _handoff(self, slot: Slot, current: AgentRecord | None, rows: dict[str, dict[str, Slot]]) -> Judged:
        if current is not None and current.generation > slot.generation and self._occupies(rows, current):
            return HANDOFF_CONFIRMED, RELEASE, current.execution_id
        return HANDOFF_UNCONFIRMED, KEEP, "successor occupancy unconfirmed"

    def _occupancy(self, slot: Slot, current: bool) -> Judged:
        seen = self._observed(slot)
        state = seen.state if seen is not None else None
        if state is observe.State.LOST:
            return ORPHAN_OCCUPANCY, RELEASE, _evidence(seen)
        if state is observe.State.SUSPECT or not current:
            return ORPHAN_OCCUPANCY, KEEP, _evidence(seen) if seen is not None else "unobserved"
        if state is observe.State.WAITING_QUOTA:
            return WAITING_QUOTA, KEEP, _evidence(seen)
        if seen is not None and seen.failure is observe.Failure.TERMINAL_LOSS:
            return TERMINAL_LOSS, KEEP, _evidence(seen)
        return None

    def _judge(
        self, slot: Slot, now: int, rows: dict[str, dict[str, Slot]], occupants: dict[str, AgentRecord]
    ) -> Judged:
        if not slot.counts(now):
            return (ENDED_SESSION if slot.state == ENDED else EXPIRED), RELEASE, ""
        seated = occupants.get(self._seat(slot.holder))
        if RETIRING in slot.holder:
            return self._handoff(slot, seated, rows)
        current = seated is not None and (seated.execution_id, seated.generation) == (
            slot.execution_id,
            slot.generation,
        )
        if slot.state == RESERVED:
            return None if current else (ORPHAN_RESERVATION, KEEP, "not the seat's current execution")
        return self._occupancy(slot, current)

    def _unaccounted(
        self, rows: dict[str, dict[str, Slot]], occupants: dict[str, AgentRecord], sessions: list[Session]
    ) -> list[Finding]:
        held = {(slot.execution_id, slot.generation) for found in rows.values() for slot in found.values()}
        current = {(agent.execution_id, agent.generation) for agent in occupants.values()}
        return [
            Finding(
                UNACCOUNTED,
                KEEP,
                "",
                f"{self.slug}/{record.seat}",
                record.execution_id,
                record.generation,
                "live session holds no slot",
            )
            for record in sessions
            if record.state != CLOSED
            and record.scope.backend != LOCAL
            and (record.execution_id, record.generation) in current - held
        ]

    def _assess(self) -> list[tuple[Finding, Slot | None]]:
        rows, now = self._rows(), self.capacity.clock()
        occupants = self.store.execution_registry.occupants(self.slug)
        sessions = [decode_session(raw) for raw in self.store.redis.hvals(self.store.key(self.slug, "fleet-sessions"))]
        assessed: list[tuple[Finding, Slot | None]] = []
        for account, found in sorted(rows.items()):
            for name, slot in sorted(found.items()):
                judged = self._judge(slot, now, rows, occupants)
                if judged is not None:
                    kind, action, evidence = judged
                    finding = Finding(kind, action, account, name, slot.execution_id, slot.generation, evidence)
                    assessed.append((finding, slot))
        return assessed + [(finding, None) for finding in self._unaccounted(rows, occupants, sessions)]

    def report(self) -> list[Finding]:
        """Read only: what reconcile would do now."""
        return [finding for finding, _ in self._assess()]

    def _release(self, finding: Finding, slot: Slot) -> Finding:
        if self.mode == OBSERVE_ONLY:
            return replace(finding, action=HELD_BACK)
        return finding if self.capacity.drop(finding.account, [slot]) else replace(finding, action=KEEP)

    def reconcile(self) -> list[Finding]:
        found = [
            self._release(finding, slot) if finding.action == RELEASE else finding for finding, slot in self._assess()
        ]
        counts = Counter(finding.kind for finding in found if finding.kind in DISCREPANCIES)
        with self.store.redis.pipeline() as pipe:
            pipe.delete(self.measured)
            if counts:
                pipe.hset(self.measured, mapping=dict(counts))
            pipe.execute()
        return found

    def exited(self, execution_id: str, generation: int, source: str) -> Finding:
        """A runtime exit frees only the row of that exact execution and generation, and only when the stored
        observation of that generation shows the exit; terminal loss is no exit."""
        if source not in EXIT_VALUES:
            raise SwarmError("insufficient_evidence")
        for account, found in sorted(self._rows().items()):
            for name, slot in found.items():
                if (slot.execution_id, slot.generation) == (execution_id, generation):
                    if not _shows_exit(self._observed(slot), source):
                        raise SwarmError("insufficient_evidence")
                    return self._release(
                        Finding(EXITED, RELEASE, account, name, execution_id, generation, source), slot
                    )
        self.store.redis.incr(self.stale_exits)
        return Finding(STALE_EXIT, KEEP, execution_id=execution_id, generation=generation, evidence=source)

    def account_occupancy_discrepancies(self) -> dict[str, int]:
        return {kind: int(count) for kind, count in self.store.redis.hgetall(self.measured).items()}

    def account_occupancy_discrepancies_total(self) -> int:
        return sum(self.account_occupancy_discrepancies().values())

    def stale_exit_events(self) -> int:
        return int(self.store.redis.get(self.stale_exits) or 0)


def page(store: RedisStore, slug: str) -> dict:
    reconciler = AccountReconciler(store, slug)
    found = reconciler.report()
    return {
        "mode": reconciler.mode,
        "discrepancies": sum(finding.kind in DISCREPANCIES for finding in found),
        "findings": [asdict(finding) for finding in found],
    }
