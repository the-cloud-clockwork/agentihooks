import hashlib
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any, Protocol

from scripts.events.bus import Bus, Delivery, StateConflict
from scripts.events.config import QueuePolicy
from scripts.events.dead import dead_letter
from scripts.events.envelope import Envelope
from scripts.hive import account_slots, admission, execution_events, headroom, presence
from scripts.swarm import capacity, launch_check, offer_quota, readiness
from scripts.swarm_v2.runtime.base import NamedRuntime, SpawnRequest, Status

ASKED = "asked"


class SpawnUnavailable(RuntimeError):
    """A spawn the runtime refused for want of room or a seat; ``reason`` is the runtime's refusal."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def refusal(error: Exception) -> str:
    """The runtime refusal a failed spawn carries, empty for any other failure."""
    return error.reason if isinstance(error, SpawnUnavailable) else ""


class Tasks(Protocol):
    def request(self, event: Envelope) -> SpawnRequest | None:
        """The task carries admitted execution_id, execution_generation and claim_generation."""
        ...

    def reopen(self, event: Envelope, refused: str = "") -> None:
        """Asks the hub to reopen a task whose launch failed; ``refused`` is the runtime's refusal, which holds the
        task off this hive until its capacity changes."""
        ...

    def dead_lettered(self, event: Envelope) -> None:
        """Asks the hub to reopen a dead lettered offer, which advances its task's claim generation when no
        admission is held, so the next offer carries a fresh claim key."""
        ...

    def failed(self, event: Envelope, harness: str, reason: str) -> None: ...

    def launched(self, event: Envelope, placed: Any) -> None: ...


@dataclass(frozen=True)
class SpokeConfig:
    swarm: Any
    lane: str
    hive: str
    policy: QueuePolicy = QueuePolicy()
    fit: Callable[[Envelope], bool] = readiness.anywhere
    accounts: Callable[..., list[capacity.Account]] | None = None


def claim_key(event: Envelope) -> str:
    return hashlib.sha256(json.dumps([event.swarm, event.key, event.version]).encode()).hexdigest()


class Spoke:
    def __init__(
        self,
        config: SpokeConfig,
        bus: Bus,
        runtime: NamedRuntime,
        tasks: Tasks,
        measure: Callable[[], dict | None] | None = None,
        clock: Callable[[], int] | None = None,
        view: Callable[[], readiness.Snapshot] | None = None,
    ) -> None:
        self.config, self.bus, self.runtime, self.tasks = config, bus.for_swarm(config.swarm.slug), runtime, tasks
        self._previous = None
        self._claimed_seen: tuple[int, int] | None = None
        self.measure = measure or self._measure
        self.clock = clock or presence.now_ms
        source = readiness.Source()
        self.view = view or (lambda: source.snapshot(config.swarm.slug))
        self._pending_key = hashlib.sha256(
            json.dumps([config.swarm.slug, config.lane, config.hive]).encode()
        ).hexdigest()

    def _measure(self) -> dict | None:
        sample = headroom.measure(0, time.time_ns() // 1_000_000, previous=self._previous)
        if sample is not None:
            self._previous = sample
        return sample

    def poll(self, timeout_ms: int = 0) -> str:
        try:
            with presence.Heartbeat(self.bus, self.config.hive, self.clock) as heartbeat:
                return self._poll(timeout_ms, heartbeat)
        except Exception:
            return self._failed()

    def _failed(self) -> str:
        exc = sys.exception()
        swarm, lane = self.config.swarm.slug, self.config.lane
        print(f"hive spoke for swarm {swarm} lane {lane} poll failed ({type(exc).__name__}: {exc})", file=sys.stderr)
        return "retry"

    def _poll(self, timeout_ms: int, heartbeat: presence.Heartbeat) -> str:
        recovered = self._recover(heartbeat)
        if recovered in ("busy", "uncertain", ASKED):
            return recovered
        heartbeat.check()
        if admission.room(self.measure()).room < 1:
            return recovered or "held"
        fit = offer_quota.Fit(self.config.swarm, self.config.lane, self.config.fit, self.config.accounts)
        try:
            queue = self.bus.queue(readiness.queue_name(self.config.swarm.slug, self.config.lane), self.config.policy)
            delivery = readiness.take(queue, self.view, timeout_ms, fit)
        except Exception:
            return self._failed()
        if delivery is None:
            return recovered or ("held" if fit.held else "empty")
        try:
            heartbeat.check()
            return self._apply(delivery, heartbeat)
        except Exception:
            self._failed()
            return self._retry(delivery)

    def _retry(self, delivery: Delivery) -> str:
        try:
            if delivery.attempt >= self.config.policy.max_deliver:
                return self._exhaust(delivery)
        except Exception:
            self._failed()
        steps = self.config.policy.backoff_ms or (self.config.policy.ack_wait_ms,)
        delivery.nak(steps[min(delivery.attempt - 1, len(steps) - 1)])
        return "retry"

    def _reserve(self, delivery: Delivery) -> int:
        pending = self.bus.state("spoke-pending")
        entry = pending.get(self._pending_key)
        if entry and json.loads(entry.value):
            raise StateConflict
        value = json.dumps({"event": delivery.event.to_dict(), "attempt": delivery.attempt}).encode()
        return (
            pending.update(self._pending_key, value, entry.revision)
            if entry
            else pending.create(self._pending_key, value)
        )

    def _clear_pending(self, revision: int) -> None:
        self.bus.state("spoke-pending").update(self._pending_key, b"{}", revision)

    def _waiting(self, prior: dict) -> str | None:
        state = prior.get("state")
        if state not in ("claimed", "launching", ASKED):
            return None
        if presence.lost(self.bus, prior.get("owner"), self.clock()):
            return None
        return "uncertain" if state == "launching" else "busy"

    def _end_leftover(self, name: str) -> bool:
        found = self.runtime.find(name)
        return found.ok and (found.value is None or self.runtime.reap(name).ok)

    def _first_seen(self, revision: int) -> int:
        """Records the clock time this spoke first sees its own claimed claim under a pending reservation and returns
        it for every later sight of that reservation, so recovery can wait out one admit ask timeout, the bound on the
        ask that wrote the claim, before it asks the hub again."""
        if self._claimed_seen is None or self._claimed_seen[0] != revision:
            self._claimed_seen = (revision, self.clock())
        return self._claimed_seen[1]

    def _recover(self, heartbeat: presence.Heartbeat) -> str | None:
        pending = self.bus.state("spoke-pending").get(self._pending_key)
        if pending is None or not (record := json.loads(pending.value)):
            return None
        event = Envelope.from_dict(record["event"])
        entry = self.bus.state("spoke-work").get(claim_key(event))
        state = json.loads(entry.value)
        status = state["state"]
        if status in (ASKED, "claimed") and state.get("owner") == self.config.hive:
            if (
                status == "claimed"
                and self.clock() - self._first_seen(pending.revision) < account_slots.REQUEST_TIMEOUT_MS
            ):
                return "busy"
            return self._ask_again(event, record["attempt"], entry.revision, pending.revision, heartbeat)  # pyright: ignore[reportOptionalMemberAccess]
        if status == "launching" and state.get("owner") == self.config.hive:
            if not self._end_leftover(state["name"]):
                return "uncertain"
            if record["attempt"] < self.config.policy.max_deliver:
                self.tasks.reopen(event)
            self.bus.state("spoke-work").update(
                claim_key(event), json.dumps({"state": "ready"}).encode(), entry.revision
            )
            status = "ready"
        if status == "started":
            self._publish(Envelope.from_dict(state["event"]))
            self._report_launch(event, state.get("launch"))
        elif status == "dead_pending" or (status == "ready" and record["attempt"] >= self.config.policy.max_deliver):
            self._finish_dead(event, record["attempt"])
            status = "dead"
        elif status == "launching":
            return "uncertain"
        elif status == "claimed":
            return "busy"
        self._clear_pending(pending.revision)
        return None if status in ("ready", ASKED) else status

    def _apply(self, delivery: Delivery, heartbeat: presence.Heartbeat) -> str:
        event = delivery.event
        claims = self.bus.state("spoke-work")
        key = claim_key(event)
        entry = claims.get(key)
        prior = json.loads(entry.value) if entry else {}
        if prior.get("state") == "started":
            self._publish(Envelope.from_dict(prior["event"]))
            self._clear_current(event)
            delivery.ack()
            self._report_launch(event, json.loads(entry.value).get("launch") if entry else None)
            return "started"
        if waiting := self._waiting(prior):
            delivery.nak(self.config.policy.ack_wait_ms)
            return waiting
        if prior.get("state") == "dead":
            return self._ended(delivery)
        if prior.get("state") == "dead_pending":
            return self._exhaust(delivery)
        if entry and prior["state"] not in ("ready", "claimed", "launching", ASKED, "withdrawn"):
            delivery.nak(self.config.policy.ack_wait_ms)
            return "busy"
        value = json.dumps({"owner": self.config.hive, "state": "claimed"}).encode()
        try:
            revision = claims.update(key, value, entry.revision) if entry else claims.create(key, value)
        except StateConflict:
            delivery.nak(self.config.policy.ack_wait_ms)
            return "busy"
        try:
            pending_revision = self._reserve(delivery)
        except Exception:
            claims.update(key, json.dumps({"state": "ready"}).encode(), revision)
            raise
        try:
            spawned = self._spawn(event, key, revision, heartbeat)
        except (admission.Unanswered, admission.Held) as unsettled:
            delivery.nak(self.config.policy.ack_wait_ms)
            return self._unsettled(unsettled, pending_revision)
        except Exception as error:
            self._give_back(event, key, pending_revision, delivery.attempt, refusal(error))
            raise
        if spawned is None:
            self._clear_pending(pending_revision)
            delivery.term()
            return "withdrawn"
        started, launch = spawned
        self._publish(started)
        self._clear_pending(pending_revision)
        delivery.ack()
        self._report_launch(event, launch)
        return "started"

    def _ended(self, delivery: Delivery) -> str:
        """Terms an offer whose claim at this generation is dead after asking the hub to advance the task's claim
        generation, so a task whose offer dead lettered unadmitted is offered again under a fresh claim key."""
        self.tasks.dead_lettered(delivery.event)
        delivery.term()
        return "dead"

    def _ask_again(
        self, event: Envelope, attempt: int, revision: int, pending_revision: int, heartbeat: presence.Heartbeat
    ) -> str:
        """Asks the hub again for the admission an unanswered admit ask left pending, or an admission wait that ended
        with its claim still claimed, and launches it, with no wait for a redelivery of the offer the hub readiness
        pass withdraws once that admission claims the task; an ask still unanswered keeps the offer pending for the
        next poll, a held answer returns it and a refusal withdraws it."""
        key = claim_key(event)
        try:
            spawned = self._spawn(event, key, revision, heartbeat)
        except (admission.Unanswered, admission.Held) as unsettled:
            return self._unsettled(unsettled, pending_revision)
        except Exception as error:
            self._give_back(event, key, pending_revision, attempt, refusal(error))
            raise
        if spawned is None:
            self._clear_pending(pending_revision)
            return "withdrawn"
        started, launch = spawned
        self._publish(started)
        self._clear_pending(pending_revision)
        self._report_launch(event, launch)
        return "started"

    def _unsettled(self, unsettled: admission.Unanswered | admission.Held, pending_revision: int) -> str:
        """An admit ask the hub left unanswered keeps the offer as this hive's pending admission, asked again on the
        next poll; a held answer drops it so another hive may take the offer."""
        if isinstance(unsettled, admission.Held):
            self._clear_pending(pending_revision)
            return admission.HELD
        self._failed()
        return ASKED

    def _give_back(self, event: Envelope, key: str, pending_revision: int, attempt: int, refused: str = "") -> None:
        """Asks the hub to reopen an offer whose launch failed and left its claim ready while the offer has deliveries
        left, carrying the runtime's refusal when there is one; the final delivery is dead lettered instead."""
        current = json.loads(self.bus.state("spoke-work").get(key).value)
        if current["state"] == "ready" and attempt < self.config.policy.max_deliver:
            self._clear_pending(pending_revision)
            if refused:
                self.tasks.reopen(event, refused)
            else:
                self.tasks.reopen(event)

    def _report_launch(self, event: Envelope, launch: dict[str, Any] | None) -> None:
        """Reports the stored launch decision to the hub once the started event is out; a refused or slow report is
        logged and never undoes the start, and a claim stored without one reports nothing."""
        if launch is None:
            return
        try:
            self.tasks.launched(event, SimpleNamespace(**launch))
        except Exception as exc:
            swarm, lane = self.config.swarm.slug, self.config.lane
            print(
                f"hive spoke for swarm {swarm} lane {lane} launch report failed ({type(exc).__name__}: {exc})",
                file=sys.stderr,
            )

    def _spawn(
        self, event: Envelope, key: str, revision: int, heartbeat: presence.Heartbeat
    ) -> tuple[Envelope, dict[str, Any]] | None:
        claims = self.bus.state("spoke-work")
        try:
            request = self.tasks.request(event)
            if request is not None:
                execution_id = request.task["execution_id"]
                generation = request.task["execution_generation"]
                task_generation = request.task["claim_generation"]
            heartbeat.check()
        except Exception as error:
            asked = isinstance(error, admission.Unanswered)
            unsettled = {"state": ASKED, "owner": self.config.hive} if asked else {"state": "ready"}
            claims.update(key, json.dumps(unsettled).encode(), revision)
            raise
        if request is None:
            claims.update(key, json.dumps({"state": "withdrawn"}).encode(), revision)
            return None
        intent = json.dumps({"state": "launching", "owner": self.config.hive, "name": request.name}).encode()
        revision = claims.update(key, intent, revision)
        request = replace(
            request,
            task={
                **request.task,
                "claim_fence": {"key": key, "revision": revision},
                "account_slot_hive": self.config.hive,
            },
        )
        execution_events.prepare(self.bus, event.swarm, request.name, generation)
        outcome = self.runtime.spawn(request)
        if not outcome.ok:
            claims.update(key, json.dumps({"state": "ready"}).encode(), revision)
            if outcome.status is Status.UNAVAILABLE:
                raise SpawnUnavailable(f"spawn {outcome.status}: {outcome.detail}", outcome.detail)
            harness = outcome.value.get("harness", "") if isinstance(outcome.value, dict) else ""
            self.tasks.failed(event, harness, outcome.detail)
            raise RuntimeError(f"spawn {outcome.status}: {outcome.detail}")
        placed = outcome.value
        started = Envelope(
            id=f"started:{key}",
            type="execution.started",
            swarm=event.swarm,
            entity="execution",
            key=execution_id,
            version=generation,
            time=time.time_ns() // 1_000_000,
            author=self.config.hive,
            data={
                "task": event.key,
                "task_generation": task_generation,
                "claim_key": key,
                "claim_revision": revision,
                "generation": generation,
                "lane": self.config.lane,
                "hive": self.config.hive,
                "name": request.name,
                "pane_id": placed.pane_id,
                "launched_at": placed.launched_at or self.clock(),
                "harness": placed.harness,
                "account": placed.account,
                "conversation_id": getattr(placed, "conversation_id", ""),
                "overlays": list(getattr(placed, "overlays", ())),
            },
            cause=event.id,
        )
        launch: dict[str, Any] = {field: getattr(placed, field, "") for field in account_slots.LAUNCH_DECISION}
        if hasattr(placed, "overlays"):
            overlays = list(placed.overlays)
            declared = launch_check.declared(launch["profile"], overlays)
            launch["overlays"] = overlays
            launch["profile_decision"] = {**placed.profile_decision, launch_check.HOST_OVERLAYS: declared}
        claims.update(
            key,
            json.dumps(
                {"state": "started", "claim_revision": revision, "event": started.to_dict(), "launch": launch}
            ).encode(),
            revision,
        )
        return started, launch

    def _publish(self, event: Envelope) -> None:
        execution_events.started(self.bus, event)

    def _clear_current(self, event: Envelope) -> None:
        pending = self.bus.state("spoke-pending").get(self._pending_key)
        if pending and json.loads(pending.value).get("event") == event.to_dict():
            self._clear_pending(pending.revision)

    def _finish_dead(self, event: Envelope, attempt: int) -> None:
        claims = self.bus.state("spoke-work")
        key = claim_key(event)
        entry = claims.get(key)
        prior = json.loads(entry.value)
        if prior["state"] == "dead_pending":
            letter = Envelope.from_dict(prior["event"])
            revision = entry.revision
        elif prior["state"] == "ready":
            letter = dead_letter(event, readiness.queue_name(event.swarm, self.config.lane), attempt)
            value = json.dumps({"state": "dead_pending", "event": letter.to_dict()}).encode()
            revision = claims.update(key, value, entry.revision)
        else:
            raise StateConflict
        self.bus.dead.publish(f"dead.{event.swarm}", letter)
        self.tasks.dead_lettered(event)
        claims.update(key, json.dumps({"state": "dead"}).encode(), revision)

    def _exhaust(self, delivery: Delivery) -> str:
        self._finish_dead(delivery.event, delivery.attempt)
        self._clear_current(delivery.event)
        delivery.term()
        return "dead"
