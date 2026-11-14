"""Lane work admission the hub answers for hive spokes: a member's Redis user cannot claim a task, mint an agent name
or occupy a seat, and a remote hive makes no service ledger reads."""

import json
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast

from packaging.version import InvalidVersion, Version

from scripts import updater
from scripts.hive import account_slots
from scripts.hive import admission as hive_admission
from scripts.hub import branch_heads, lease
from scripts.swarm import readiness, run_window, successor_fill
from scripts.swarm.execution import TERMINAL_STATES
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import AgentRecord, RedisStore, connect

ADMITTED = "spoke-admission"
# Long enough for every redelivery of one offer; a reopen raises the claim generation, so a new offer has a new key.
ADMITTED_MS = 24 * 60 * 60 * 1000
ANSWER_MS = account_slots.REQUEST_TIMEOUT_MS - 2_000
TURN_MS, TURN_TRIES, TURN_WAIT_S = 30_000, 40, 0.05  # pragma: no mutate
BUSY = "another admission for the hive is in progress"


def release_key(text: str) -> Version | None:
    """The PEP 440 release a version string names; None for an unreported or unparsable one."""
    try:
        return Version(text)
    except InvalidVersion:
        return None


class LateAdmission(RuntimeError):
    """An admission finished after the asking hive stopped waiting for its answer."""


def refused(slug: str, request: dict[str, Any], reason: str) -> None:
    """Prints the one line naming the member hive, the task and the reason a lane admission is refused."""
    print(
        f"hub: events admission swarm={slug} task={request['task']} hive={request['hive']} refused: {reason}",
        file=sys.stderr,
    )


class Admission:
    def __init__(
        self,
        store: RedisStore | None = None,
        ledger: LedgerClient | None = None,
        source: readiness.Source | None = None,
        clock: lease.Clock = lease.wall_ms,
    ):
        self._store, self.ledger, self.clock = store, ledger or LedgerClient(service=True), clock
        self.source = source or readiness.Source(store, self.ledger)

    @property
    def store(self) -> RedisStore:
        self._store = self._store or connect()
        return self._store

    def _key(self, slug: str, request: dict) -> str:
        return self.store.key(slug, ADMITTED, request["task"], str(request["generation"]))

    def admit(self, slug: str, request: dict[str, Any]) -> dict | None:
        started = self.clock()
        if older := self._outdated(request["hive"]):
            refused(slug, request, older)
            return None
        held = self.store.redis.get(self._key(slug, request))
        if held:
            admitted = json.loads(held)
            if admitted["hive"] == request["hive"]:
                return admitted
            refused(slug, request, f"already admitted to hive {admitted['hive']}")
            return None
        if run_window.claims_stopped(self.store, slug, self.store.config(slug), self.clock()):
            refused(slug, request, "claims are stopped")
            return None
        task_id, lane, generation = request["task"], request["lane"], request["generation"]
        view = self.source.snapshot(slug)
        if withdrawn := readiness.withdrawal(
            SimpleNamespace(key=task_id, version=generation, data={"lane": lane}), view
        ):
            refused(slug, request, f"the offer is withdrawn, {withdrawn}")
            return None
        if unplaceable := self._unplaceable(view.rows[task_id], request["hive"]):
            print(
                f"hub: events placement swarm={slug} task={task_id} hive={request['hive']}: refused, {unplaceable}",
                file=sys.stderr,
            )
            return None
        from scripts.swarm import tick

        if reason := tick._lives_spent(slug, self.store, self.ledger, view.rows, view.rows[task_id]):
            print(
                f"hub: events claim cap swarm={slug} task={task_id} hive={request['hive']}: {reason}", file=sys.stderr
            )
            return None
        with self._turn(request["hive"]) as busy:
            if full := busy or self._full(request["hive"]):
                refused(slug, request, full)
                held: dict[str, str] = {hive_admission.HELD: full}
                return held
            admitted = self._claim(slug, request, view.rows, view.doc)
        if admitted is None:
            refused(slug, request, "another holder took the claim")
            return None
        return self._answered(slug, request, started, admitted)

    def _answered(self, slug: str, request: dict[str, Any], started: int, admitted: dict[str, Any]) -> dict[str, Any]:
        """The admission the asking hive is still waiting for; one finished past its wait is released and refused."""
        elapsed = self.clock() - started
        if elapsed < ANSWER_MS:
            return admitted
        self.reopen(slug, request)
        print(
            f"hub: events admission swarm={slug} task={request['task']} hive={request['hive']} late: "
            f"finished after {elapsed} ms, the hive stopped waiting at {ANSWER_MS} ms",
            file=sys.stderr,
        )
        raise LateAdmission(f"admission of {request['task']} finished after hive {request['hive']} stopped waiting")

    def _outdated(self, hive: str) -> str:
        """The member release below the hub release it is refused for; empty when either release is unreported."""
        from scripts.hive import registry

        record: Any = registry.show(self.store.redis, hive) or {}  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType, reportUnknownArgumentType]
        member, hub = str(record.get("version")), updater.installed_version()
        reported, floor = release_key(member), release_key(hub)
        return f"release {member} below hub release {hub}" if reported and floor and reported < floor else ""

    def _unplaceable(self, row: dict[str, Any], hive: str) -> str:
        """Why a draining hive, a workstation task off the root hive, a task pinned to another hive or one needing what the hive lacks is
        never admitted; empty when the hive may take the task."""
        from scripts.hive import placement, registry

        record: Any = registry.show(self.store.redis, hive) or {}  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType, reportUnknownArgumentType]
        if record.get("draining") == placement.YES:
            return "the hive is draining"
        if not readiness.placeable(row, hive, readiness.declared(self.store, hive)):
            return "the task is workstation off the root hive, pinned to another hive or needs what the hive lacks"
        return ""

    @contextmanager
    def _turn(self, hive: str) -> Iterator[str]:
        """Holds the hive's admission key in Redis, shared by every hub pod, while its cap is counted and the task
        claimed; yields BUSY when another admission for the hive keeps it past the wait."""
        from scripts.hive import registry

        key, token = f"{registry.key(hive)}:admitting", uuid.uuid4().hex
        for _ in range(TURN_TRIES):
            if self.store.redis.set(key, token, nx=True, px=TURN_MS):
                break
            time.sleep(TURN_WAIT_S)
        else:
            yield BUSY
            return
        try:
            yield ""
        finally:
            if self.store.redis.get(key) == token:
                self.store.redis.delete(key)

    def _full(self, hive: str) -> str:
        """The hive's live worker executions across every swarm against its registry max agents; empty below the cap
        or when none is set."""
        from scripts.hive import registry

        record: Any = registry.show(self.store.redis, hive) or {}  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType, reportUnknownArgumentType]
        cap = record.get("max_agents")
        if cap is None:
            return ""
        slugs = cast(list[str], self.store.slugs())  # pragma: no mutate
        hosts = {hive, ""} if hive == readiness.ROOT else {hive}
        live = sum(
            1
            for slug in slugs
            for agent in self.store.execution_registry.agents(slug)
            if agent.hive in hosts and agent.lane != "master" and agent.state not in TERMINAL_STATES
        )
        return f"{live} of {cap} agents on the hive" if live >= cap else ""

    def _claim(
        self, slug: str, request: dict[str, Any], rows: dict[str, Any], doc: dict[str, Any]
    ) -> dict[str, Any] | None:
        from scripts.hive import orders
        from scripts.swarm import profile_choice, tick
        from scripts.swarm_ledger import ledger_workspace

        task_id, lane, now = request["task"], request["lane"], self.clock()
        row = rows[task_id]
        name = self.store.next_name(slug, lane, now)
        if not self.store.claim(slug, task_id, name, tick.LEASE_MS):
            return None
        taken = {agent.seat for agent in self.store.agents(slug)}
        seat = tick._free_seat(slug, lane, taken, self.store.handoff_seat(slug, task_id))
        record = AgentRecord(name, lane, task_id, seat=seat, state="starting", started_at=now, hive=request["hive"])
        previous = self.store.execution_occupants(slug).get(seat)
        try:
            agent = self.store.start_execution(slug, record, previous.execution_id if previous else "")
        except Exception:
            self._unclaim(slug, task_id, name)
            raise
        live = self._mark(slug, request, row, name)
        if live is None:
            return None
        self.store.pend_life(slug, task_id, json.dumps([agent.execution_id, agent.generation]))
        self.store.seats.occupy(seat, name, now)
        claimed: dict[str, Any] = {**row, **live}
        claimed.update(self._continuation(slug, name, claimed, rows))
        claimed["overlaps"] = tick._sharing(claimed, rows, self.store.agents(slug))
        members: list[str] = claimed.get("group_members") or []
        claimed["group"] = [
            {key: rows[member][key] for key in ("id", "title", "description")} for member in members if member in rows
        ]
        claimed[orders.SEEDS] = ledger_workspace.carried(self.store, slug, claimed, doc, name)
        primed = tick.primed(self.store, slug, seat, claimed)
        task = successor_fill.task(primed, self.store.config(slug), record.lane)
        task["stack_base"] = self._stack_base(slug, task, rows)
        identity = {
            "execution_id": agent.execution_id,
            "execution_generation": agent.generation,
            profile_choice.LEDGER_INTENT: profile_choice.carried(doc, task),
        }
        avoid = self.store.failed_harness(slug, task_id)
        admitted = {
            "hive": request["hive"],
            "name": name,
            "task": {**task, **identity, **({"avoid_harness": avoid} if avoid else {})},
        }
        self.store.redis.set(self._key(slug, request), json.dumps(admitted), px=ADMITTED_MS)
        return admitted

    def _stack_base(self, slug: str, task: dict, rows: dict) -> list[dict]:
        """The hub holds no checkout, so a stored repository origin stands in for the checkout's own."""
        from scripts.swarm import stack, tick

        config = self.store.config(slug)
        if config.repo_origin:
            return tick._stack_base(task, rows, config.repo_origin, stack.repo_key)
        return tick._stack_base(task, rows, config.repo)

    def _continuation(self, slug: str, name: str, task: dict[str, Any], rows: dict[str, Any]) -> dict[str, Any]:
        """The handoff or the recorded branch a reclaimed task continues from, resolved on the claimed row so a cleared
        protected branch is never continued; empty for a first life with no branch."""
        from scripts.swarm import continuation

        task_id: str = task["id"]
        handoff: str = self.store.handoff(slug, task_id)
        lives: list[str] = self.store.earlier_lives(slug, task_id)
        if not (handoff or lives or task.get("branch")):
            return {}
        config = self.store.config(slug)
        reclaim, origin = branch_heads.reclaimer(config)
        if handoff:
            saved: dict[str, Any] = self.store.handoff_envelope(slug, task_id) or {}
            resolved = continuation.resolve(config.repo, task, rows, lives, saved, reclaim, origin)
            return {"handoff": handoff, "handoff_envelope": resolved}
        found = continuation.resolve(config.repo, task, rows, lives, {}, reclaim, origin)
        self.store.put_reclaim(slug, name, found)
        return {"reclaim": found}

    def _mark(self, slug: str, request: dict[str, Any], row: dict[str, Any], name: str) -> dict[str, Any] | None:
        """The ledger row once claimed by name with its portable work folder, a protected branch cleared and the lane
        kind; its claim generation is one past the offer's."""
        from scripts.swarm import continuation
        from scripts.swarm_ledger import ledger_workspace

        task_id: str = request["task"]
        expected = {"claimed_by": row.get("claimed_by", ""), "claim_generation": request["generation"]}
        try:
            fields = {
                "state": "pr" if row.get("pr_url") else "claimed",
                "claimed_by": name,
                "workspace": ledger_workspace.portable(slug, task_id),
            }
            if row.get("branch") in continuation.PROTECTED:
                fields["branch"] = ""
            kind: str = self.store.config(slug).lanes.get(request["lane"], {}).get("kind", "")
            if kind not in ("", "auto") and not row.get("kind"):
                fields["kind"] = kind
            live: dict[str, Any] = self.ledger.update_task(slug, task_id, fields, if_state=("open",), if_match=expected)
        except Exception:
            self._unclaim(slug, task_id, name)
            raise
        if live.get("claimed_by") != name:
            self._unclaim(slug, task_id, name)
            return None
        return live

    def _unclaim(self, slug: str, task_id: str, name: str) -> None:
        self.store.release(slug, task_id, name)
        self.store.drop_agent(slug, name)

    def reopen(self, slug: str, request: dict) -> bool:
        key = self._key(slug, request)
        held = self.store.redis.get(key)
        if not held or json.loads(held)["hive"] != request["hive"]:
            return False
        name = json.loads(held)["name"]
        self.store.cancel_pending_life(slug, request["task"])
        self._unclaim(slug, request["task"], name)
        self.ledger.update_task(
            slug,
            request["task"],
            {"state": "open", "claimed_by": ""},
            if_state=("claimed", "pr"),
            if_match={"claimed_by": name},
        )
        self.store.redis.delete(key)
        return True

    def failed(self, slug: str, request: dict) -> bool:
        key = self._key(slug, request)
        held = self.store.redis.get(key)
        admitted = json.loads(held) if held else None
        if not admitted or admitted["hive"] != request["hive"]:
            return False
        task_id, harness, reason = request["task"], request["harness"], request["reason"]
        self.store.note_launch_failure(slug, task_id, reason, harness)
        if harness:
            admitted["task"]["avoid_harness"] = harness
            self.store.redis.set(key, json.dumps(admitted), keepttl=True)
            print(f"hub: events steering swarm={slug} task={task_id} avoid={harness}: {reason}", file=sys.stderr)
        return True

    def launched(self, slug: str, request: dict[str, Any]) -> bool:
        """Writes the launch decision the admitted hive reports onto its agent's record; False when the hive holds no
        admission for the offer or another agent now occupies the admitted seat."""
        held = self.store.redis.get(self._key(slug, request))
        admitted = json.loads(held) if held else None
        if not admitted or admitted["hive"] != request["hive"]:
            return False
        occupant = self.store.execution_occupants(slug).get(admitted["task"]["seat"])
        if occupant is None or occupant.name != admitted["name"]:
            return False
        launch = request["launch"]
        decision = {field: launch[field] for field in account_slots.LAUNCH_DECISION}
        return self.store.execution_registry.set_launch(slug, occupant.execution_id, decision)


OPERATIONS = ("admit", "reopen", "failed", "launched")
