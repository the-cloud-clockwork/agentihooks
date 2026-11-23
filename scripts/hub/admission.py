"""Lane work admission the hub answers for hive spokes: a member's Redis user cannot claim a task, mint an agent name
or occupy a seat, and a remote hive makes no service ledger reads."""

import json
import sys
import time
import uuid
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast

from packaging.version import InvalidVersion, Version

from scripts import updater
from scripts.events.bus import SERVE_WAITED_MS
from scripts.handoff import transfers
from scripts.hive import account_slots
from scripts.hive import admission as hive_admission
from scripts.hub import branch_heads, lease
from scripts.swarm import readiness, run_window, successor_fill
from scripts.swarm.execution import TERMINAL_STATES
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError, connect
from scripts.swarm_ledger.ledger_workspace import WORK_FOLDER

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


class _StepTimer:
    """Milliseconds since the previous mark, one instance per admit call so concurrent calls on one Admission
    never share timing state."""

    def __init__(self, clock: lease.Clock, started: int) -> None:
        self._clock, self._last = clock, started
        self.marks: dict[str, int] = {}

    def mark(self, step: str) -> None:
        now = self._clock()
        self.marks[step] = now - self._last
        self._last = now

    def line(self) -> str:
        return " ".join(f"{step}={ms}" for step, ms in self.marks.items())


def refused(slug: str, request: dict[str, Any], reason: str) -> None:
    """Prints the one line naming the member hive, the task and the reason a lane admission is refused."""
    print(
        f"hub: events admission swarm={slug} task={request['task']} hive={request['hive']} refused: {reason}",
        file=sys.stderr,
    )


def turn_key(hive: str) -> str:
    """The Redis key every hub pod holds while it admits a launch for the hive."""
    from scripts.hive import registry

    return f"{registry.key(hive)}:admitting"


def claiming_key(offer: str) -> str:
    """The Redis key naming the hive whose admission of the offer is still claiming."""
    return f"{offer}:claiming"


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
        begun = self.clock()
        queued_ms = SERVE_WAITED_MS.get()
        started = begun - queued_ms
        timer = _StepTimer(self.clock, begun)
        if older := self._outdated(request["hive"]):
            refused(slug, request, older)
            return {hive_admission.HELD: older}
        timer.mark("release")
        offer = self._key(slug, request)
        admitted = self._held(offer)
        if admitted:
            live = self._active(slug, admitted)
            if live and admitted["hive"] == request["hive"]:
                return admitted
            if live:
                refused(slug, request, f"already admitted to hive {admitted['hive']}")
                return None
        timer.mark("held")
        if run_window.claims_stopped(self.store, slug, self.store.config(slug), self.clock()):
            refused(slug, request, "claims are stopped")
            return None
        timer.mark("stopped")
        task_id, lane, generation = request["task"], request["lane"], request["generation"]
        view = self.source.snapshot(slug)
        timer.mark("snapshot")
        if withdrawn := readiness.withdrawal(
            SimpleNamespace(key=task_id, version=generation, data={"lane": lane}), view
        ):
            return self._withdrawn(slug, request, offer, withdrawn)
        timer.mark("withdrawal")
        if unplaceable := self._unplaceable(view.rows[task_id], request["hive"]):
            print(
                f"hub: events placement swarm={slug} task={task_id} hive={request['hive']}: refused, {unplaceable}",
                file=sys.stderr,
            )
            return {hive_admission.HELD: unplaceable}
        timer.mark("placement")
        from scripts.swarm import tick

        if reason := tick._lives_spent(slug, self.store, self.ledger, view.rows, view.rows[task_id]):
            print(
                f"hub: events claim cap swarm={slug} task={task_id} hive={request['hive']}: {reason}", file=sys.stderr
            )
            return None
        timer.mark("cap")
        reserved = self._reserved(view, view.rows[task_id], request["hive"])
        with self._turn(request["hive"], offer) as busy:
            timer.mark("turn")
            if not busy and (kept := self._kept(slug, request, offer)):
                return kept
            if full := busy or self._full(request["hive"], reserved):
                refused(slug, request, full)
                held: dict[str, str] = {hive_admission.HELD: full}
                return held
            timer.mark("full")
            with self._claiming(offer, request["hive"]):
                admitted = self._claim(slug, request, view.rows, view.doc, timer)
        if admitted is None:
            refused(slug, request, "another holder took the claim")
            return None
        return self._answered(slug, request, started, admitted, timer)

    def _held(self, offer: str) -> dict[str, Any] | None:
        """The admission kept for the offer, whichever hive asked for it; None when none is kept."""
        held: str | None = self.store.redis.get(offer)  # pyright: ignore[reportUnknownMemberType]
        return json.loads(held) if held else None

    def _active(self, slug: str, admitted: dict[str, Any]) -> bool:
        if admitted["name"] in self.store.listed(slug):
            return True
        task = admitted["task"]
        occupant = self.store.execution_occupants(slug).get(task["seat"])
        return bool(
            occupant is not None
            and occupant.state == "starting"
            and not occupant.launched_at
            and (occupant.name, occupant.execution_id, occupant.generation)
            == (admitted["name"], task["execution_id"], task["execution_generation"])
            and cast(Any, self.store).claimant(slug, task["id"]) == admitted["name"]
        )

    def _kept(self, slug: str, request: dict[str, Any], offer: str) -> dict[str, Any] | None:
        """The asking hive's kept admission while its agent is listed or its unlaunched execution still holds the claim,
        and a held answer while that hive's own admission of the offer is still claiming."""
        claiming: object = self.store.redis.get(claiming_key(offer))  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if claiming == request["hive"]:
            return {hive_admission.HELD: hive_admission.ADMITTING}
        admitted = self._held(offer)
        if admitted and admitted["hive"] == request["hive"] and self._active(slug, admitted):
            return admitted
        return None

    def _withdrawn(self, slug: str, request: dict[str, Any], offer: str, withdrawn: str) -> dict[str, Any] | None:
        """A repeat ask from the hive whose own admission claimed the offer: answered with that admission once it
        leaves the hive's turn and held while it outlasts the turn wait; any other ask is refused for the withdrawal."""
        if self._admitting(request["hive"], offer):
            with self._turn(request["hive"], offer) as busy:
                if busy == hive_admission.ADMITTING:
                    held: dict[str, str] = {hive_admission.HELD: busy}
                    return held
        if kept := self._kept(slug, request, offer):
            return kept
        refused(slug, request, f"the offer is withdrawn, {withdrawn}")
        return None

    def _answered(
        self, slug: str, request: dict[str, Any], started: int, admitted: dict[str, Any], timer: _StepTimer
    ) -> dict[str, Any]:
        """The admission for the asking hive; one finished past the answer budget keeps its claim, so a hive still
        waiting gets it and a hive that stopped waiting gets the same admission when it asks again for the offer."""
        elapsed = self.clock() - started
        if elapsed >= ANSWER_MS:
            print(
                f"hub: events admission swarm={slug} task={request['task']} hive={request['hive']} late: "
                f"finished after {elapsed} ms, past the {ANSWER_MS} ms answer budget, claim kept {timer.line()}",
                file=sys.stderr,
            )
        return admitted

    def _outdated(self, hive: str) -> str:
        """The member release below the hub release it is refused for; empty when either release is unreported or the
        hive's own chart pinned it to its release."""
        from scripts.hive import registry

        record: Any = registry.show(self.store.redis, hive) or {}  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType, reportUnknownArgumentType]
        if record.get("pinned") == "yes":
            return ""
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

    def _reserved(self, view: readiness.Snapshot, row: dict[str, Any], hive: str) -> int:
        """The root slots kept for the waiting tasks only the root hive takes of every events swarm, the asking swarm
        read from its own view, when this task could run on any hive; none for another hive or a task only the root
        hive takes."""
        if hive != readiness.ROOT or readiness.root_only(row):
            return 0
        capabilities = readiness.declared(self.store, hive)
        views = [view, *(self.source.snapshot(slug) for slug in self.source.slugs() if slug != view.slug)]
        return sum(readiness.root_waiting(each, capabilities) for each in views)

    @contextmanager
    def _turn(self, hive: str, offer: str) -> Iterator[str]:
        """Holds the hive's admission key in Redis, shared by every hub pod, while its cap is counted and the task
        claimed, naming the offer it admits; yields ADMITTING when the hive's own admission of the same offer keeps it
        past the wait and BUSY when another admission for the hive does."""
        key, token = turn_key(hive), f"{offer} {uuid.uuid4().hex}"
        for _ in range(TURN_TRIES):
            if self.store.redis.set(key, token, nx=True, px=TURN_MS):
                break
            time.sleep(TURN_WAIT_S)
        else:
            yield hive_admission.ADMITTING if self._admitting(hive, offer) else BUSY
            return
        try:
            yield ""
        finally:
            if self.store.redis.get(key) == token:
                self.store.redis.delete(key)

    @contextmanager
    def _claiming(self, offer: str, hive: str) -> Generator[None]:
        """Names the hive whose admission of the offer is claiming for as long as the claim runs, within the task
        claim lease, so that hive's repeat ask stays held after its turn key lapses; cleared once the claim returns
        or raises while the mark still names that hive."""
        from scripts.swarm import tick

        key = claiming_key(offer)
        self.store.redis.set(key, hive, px=tick.LEASE_MS)  # pyright: ignore[reportUnknownMemberType]
        try:
            yield
        finally:
            if self.store.redis.get(key) == hive:  # pyright: ignore[reportUnknownMemberType]
                self.store.redis.delete(key)  # pyright: ignore[reportUnknownMemberType]

    def _admitting(self, hive: str, offer: str) -> bool:
        """True while the hive's admission key is held by an admission of this same offer."""
        holder: object = self.store.redis.get(turn_key(hive))  # pyright: ignore[reportUnknownMemberType]
        return str(holder).startswith(f"{offer} ")

    def _full(self, hive: str, reserved: int) -> str:
        """The hive's live worker executions across every swarm, with the slots it keeps for waiting tasks only it
        takes and, on a hive that takes experts, one for each running expert lineage of every swarm with no live
        expert, against its registry max agents; empty below the cap or when none is set. The tick places an expert
        successor, never this admission, so the slot kept for it holds lane offers until the expert seats step seats
        it."""
        from scripts.hive import placement, registry
        from scripts.swarm import expert_launch, expert_lineage

        record: Any = registry.show(self.store.redis, hive) or {}  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType, reportUnknownArgumentType]
        cap = record.get("max_agents")
        if cap is None:
            return ""
        slugs = cast(list[str], self.store.slugs())  # pragma: no mutate
        hosts = {hive, ""} if hive == readiness.ROOT else {hive}
        experts = placement.hosts(expert_launch.LANE, record)
        live = waiting = 0
        for slug in slugs:
            agents = self.store.execution_registry.agents(slug)
            live += sum(
                1
                for agent in agents
                if agent.hive in hosts and agent.lane != "master" and agent.state not in TERMINAL_STATES
            )
            waiting += expert_lineage.waiting(self.store, slug, agents) if experts else 0
        kept = f" with {reserved} kept for tasks only the root hive takes" if reserved else ""
        kept += f" with {waiting} kept for expert lineages waiting for an expert" if waiting else ""
        return f"{live} of {cap} agents on the hive{kept}" if live + reserved + waiting >= cap else ""

    def _claim(
        self, slug: str, request: dict[str, Any], rows: dict[str, Any], doc: dict[str, Any], timer: _StepTimer
    ) -> dict[str, Any] | None:
        from scripts.hive import orders
        from scripts.swarm import launch_check, profile_choice, tick

        task_id, lane, now = request["task"], request["lane"], self.clock()
        row = rows[task_id]
        name = self.store.next_name(slug, lane, now)
        if not self.store.claim(slug, task_id, name, tick.LEASE_MS):
            return None
        timer.mark("claim")
        taken = {agent.seat for agent in self.store.agents(slug)}
        saved = cast(Any, self.store).redis.hget(self.store.key(slug, "launch-assignments"), task_id)
        preferred = cast(str, json.loads(saved)["seat"] if saved else self.store.handoff_seat(slug, task_id))
        seat = tick._free_seat(slug, lane, taken, preferred)
        record = AgentRecord(name, lane, task_id, seat=seat, state="starting", started_at=now, hive=request["hive"])
        previous = self.store.execution_occupants(slug).get(seat)
        timer.mark("seat")
        try:
            agent = self.store.start_execution(slug, record, previous.execution_id if previous else "")
        except Exception:
            self._unclaim(slug, task_id, name)
            raise
        timer.mark("execution")
        live = self._mark(slug, request, row, name)
        timer.mark("ledger")
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
        claimed[orders.SEEDS] = self._seeds(slug, claimed, doc, name)
        primed = tick.primed(self.store, slug, seat, claimed)
        task = successor_fill.task(primed, self.store.config(slug), record.lane)
        task["stack_base"] = self._stack_base(slug, task, rows)
        identity = {
            "execution_id": agent.execution_id,
            "admitted_execution_id": agent.execution_id,
            "execution_generation": agent.generation,
            profile_choice.LEDGER_INTENT: profile_choice.carried(doc, task),
        }
        avoid = self.store.failed_harness(slug, task_id)
        try:
            task["transfer"] = transfers.attach(self.store, slug, record)
        except Exception:
            self._release(slug, task_id, name)
            raise
        admitted = {
            "hive": request["hive"],
            "name": name,
            "task": {**task, **identity, **({"avoid_harness": avoid} if avoid else {})},
        }
        self.store.redis.set(self._key(slug, request), json.dumps(admitted), px=ADMITTED_MS)
        launch_check.begin(self.store, slug, agent, now, owner=launch_check.HUB)
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
        repo = config.repo_origin or config.repo
        if handoff:
            saved: dict[str, Any] = self.store.handoff_envelope(slug, task_id) or {}
            resolved = continuation.resolve(repo, task, rows, lives, saved, reclaim, origin)
            return {"handoff": handoff, "handoff_envelope": resolved}
        found = continuation.resolve(repo, task, rows, lives, {}, reclaim, origin)
        self.store.put_reclaim(slug, name, found)
        return {"reclaim": found}

    def _seeds(self, slug: str, task: dict[str, Any], doc: dict[str, Any], name: str) -> dict[str, str]:
        """The files the hive writes into the task's work folder before the launch: steering from the task and the
        latest task copy synced by the hive that last launched the task, else the one synced by the hive of its
        previous agent, so a predecessor retired before this admission still hands over its lines."""
        from scripts.swarm_ledger import ledger_workspace

        earlier = [agent for agent in self.store.agents(slug) if agent.task == task["id"] and agent.name != name]
        previous = max(earlier, key=lambda agent: agent.started_at, default=None)
        redis = cast(Any, self.store).redis
        hives = (redis.get(self.store.key(slug, WORK_FOLDER, task["id"])), previous and previous.hive)
        copies = (redis.get(ledger_workspace.task_files_key(hive, slug, task["id"])) for hive in hives if hive)
        held = next(filter(None, copies), None)
        synced: dict[str, str] = json.loads(held) if held else {}
        return {**synced, "steering.md": ledger_workspace.steering(task, doc)}

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
        """Reopens the task the asking hive's admission claimed and forgets that admission, answering True only then;
        with no admission held, a dead lettered offer advances its task's claim generation instead."""
        key = self._key(slug, request)
        held = self.store.redis.get(key)
        if not held:
            if request.get("dead"):
                self._advance(slug, request)
            return False
        if json.loads(held)["hive"] != request["hive"]:
            return False
        self._release(slug, request["task"], json.loads(held)["name"])
        self.store.redis.delete(key)
        return True

    def _advance(self, slug: str, request: dict[str, Any]) -> None:
        """Raises the claim generation of an open, unclaimed task whose offer dead lettered with no admission held, so
        its next offer carries a fresh claim key; a task claimed or already past that generation is left as it is,
        and a ledger that refuses or cannot take the advance is logged, since the spoke asks again when a later
        delivery meets the dead claim."""
        generation: int = request["generation"]
        try:
            self.ledger.update_task(
                slug,
                request["task"],
                {"claim_generation": generation + 1},
                if_state=("open",),
                if_match={"claimed_by": "", "claim_generation": generation},
            )
        except SwarmError as exc:
            print(
                f"hub: events reopen swarm={slug} task={request['task']} generation={generation}: "
                f"claim generation not advanced, {exc}",
                file=sys.stderr,
            )

    def _release(self, slug: str, task_id: str, name: str) -> None:
        """Returns a claimed task to open: its pending life, its transfer, its claim and agent, then its ledger row."""
        self.store.cancel_pending_life(slug, task_id)
        transfers.detach(self.store, slug, SimpleNamespace(name=name))
        self._unclaim(slug, task_id, name)
        self.ledger.update_task(
            slug,
            task_id,
            {"state": "open", "claimed_by": ""},
            if_state=("claimed", "pr"),
            if_match={"claimed_by": name},
        )

    def free(self, slug: str, agent: AgentRecord) -> None:
        """Returns the claimed task of an admitted agent whose hive never started it to open, releasing its hive slot,
        claim and seat."""
        from scripts.hive.launch import release

        release(self.store, slug, agent)
        self._release(slug, agent.task, agent.name)

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
        """Writes the launch decision the admitted hive reports onto its agent's record, with the overlays and profile
        decision the hive placed, and records that hive as the holder of the task's work folder for the next agent's
        seeds; the hub launch check admission started stays as it is, so a late report never restarts a judged check.
        False when the hive holds no admission for the offer or another agent now occupies the admitted seat."""
        held = self.store.redis.get(self._key(slug, request))
        admitted = json.loads(held) if held else None
        if not admitted or admitted["hive"] != request["hive"]:
            return False
        occupant = self.store.execution_occupants(slug).get(admitted["task"]["seat"])
        if occupant is None or occupant.name != admitted["name"]:
            return False
        launch = request["launch"]
        decision = {field: launch[field] for field in account_slots.LAUNCH_DECISION}
        placed = {field: launch[field] for field in account_slots.LAUNCH_PLACEMENT if field in launch}
        if not self.store.execution_registry.set_launch(slug, occupant.execution_id, {**decision, **placed}):
            return False
        folder = self.store.key(slug, WORK_FOLDER, request["task"])
        cast(Any, self.store).redis.set(folder, request["hive"])
        cast(Any, self.store).redis.hdel(self.store.key(slug, "launch-assignments"), request["task"])
        return True


OPERATIONS = ("admit", "reopen", "failed", "launched")
