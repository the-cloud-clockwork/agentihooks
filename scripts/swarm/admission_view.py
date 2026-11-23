"""A hub admission's own view of one swarm, read row by row instead of through the whole ledger export: the task, the
rows it names, the running rows, its overview and phase, and the claim gate verdict the ledger judges over its stored
state, so no gate is judged on a partial document."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any

from scripts.swarm import apply_wait, claim_hold, fleet_red, freeze, readiness, release_wait
from scripts.swarm.health import focus_check
from scripts.swarm.store import RedisStore, SwarmConfig

NAMES = ("depends_on", "parked_on", "group_members")
LINKS = ("enhances", "merged_into")
RUNNING = ("claimed", "pr")
BRANCH_FIELDS = ("branch", "branch_repo", "pr_url")
GONE = "the task is gone"
DONE = "it is done"
Mark = Callable[[str], None]
Claims = Callable[[RedisStore, str], Callable[[str], bool]]


@dataclass(frozen=True)
class View:
    """The snapshot the readiness checks judge, the document the claim seeds read and the ledger's hold verdict."""

    snapshot: readiness.Snapshot
    doc: dict[str, Any]
    held: Callable[[dict[str, Any]], str]

    @property
    def rows(self) -> dict[str, Any]:
        return self.snapshot.rows


def claimed(store: Any, slug: str) -> Callable[[str], bool]:
    """A task an unfinished agent of the swarm holds or the store names a claimant for."""
    owned: set[str] = {agent.task for agent in store.agents(slug) if agent.state != "finished"}
    return lambda task_id: task_id in owned or store.claimant(slug, task_id) is not None


def admits(store: RedisStore, slug: str, config: SwarmConfig, rows: dict[str, Any]) -> Callable[[dict[str, Any]], bool]:
    """The running swarm's release, apply, fleet red and lab checks on one task."""
    released = release_wait.checker(config)
    applied: Callable[[dict[str, Any]], bool] = apply_wait.checker(config, rows)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    unshared = fleet_red.checker(fleet_red.saved_reader(store, slug))
    lab = claim_hold.lab_reason(store, slug, freeze.fix_phase(config))
    return lambda task: (
        config.state == "running" and released(task) and applied(task) and unshared(task) and not lab(task)
    )


def verdict(found: dict[str, Any] | None, task_id: str) -> str:
    """The reason the ledger's claim gate holds one task; empty when it is claimable."""
    if found is None:
        return GONE
    if task_id in found["done"]:
        return DONE
    judged = [row["verdict"] for row in found["tasks"] if row["id"] == task_id]
    if not judged:
        return GONE
    return "" if judged[0] == focus_check.CLAIMABLE else str(judged[0])


def withdrawal(event: Any, view: View) -> str:
    """Why an offer no longer stands: the snapshot's own checks first, then the ledger's claim gate verdict."""
    return readiness.withdrawal(event, view.snapshot) or view.held(view.rows[event.key])


def named(rows: list[dict[str, Any]]) -> set[str]:
    """The ids the rows' dependencies, parks, group members, enhancement target and merge target name."""
    listed = {str(each) for row in rows for key in NAMES for each in row.get(key) or ()}
    return listed | {str(row[key]) for row in rows for key in LINKS if row.get(key)}


class Narrow:
    """Builds an admission's view of one swarm from narrow ledger reads, the whole document kept only for the steering
    evidence of a plan task."""

    def __init__(
        self, store: Callable[[], RedisStore], ledger: Any, full: readiness.Source, claims: Claims = claimed
    ) -> None:
        self._store, self._ledger, self._full, self._claims = store, ledger, full, claims

    def task(self, slug: str, task_id: str, mark: Mark) -> View:
        """The view an admission of one task judges and claims from, marking each read it times."""
        row: dict[str, Any] | None = self._ledger.task(slug, task_id)
        mark("row")
        found = [row] if row else []
        rows = self._related(slug, found)
        rows.update({each["id"]: each for each in self._ledger.tasks_where(slug, states=RUNNING)})
        rows.update({each["id"]: each for each in found})
        mark("rows")
        doc = self._doc(slug, row, rows)
        mark("parts")
        return View(self._snapshot(slug, rows), doc, self._held(slug))

    def waiting(self, slug: str, capabilities: Callable[[], Iterable[str]]) -> int:
        """The swarm's open tasks only the root hive takes and can place that its claim gate would let start now."""
        pinned: list[dict[str, Any]] = self._ledger.tasks_where(slug, states=("open",), pinned=True)
        candidates: list[dict[str, Any]] = [row for row in pinned if readiness.root_only(row)]
        if not candidates:
            return 0
        rows = self._related(slug, candidates)
        rows.update({each["id"]: each for each in candidates})
        snapshot, held = self._snapshot(slug, rows), self._held(slug)
        checks: Callable[[dict[str, Any]], bool] = snapshot.admits  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

        def admitted(task: dict[str, Any]) -> bool:
            return checks(task) and not held(task)

        return readiness.root_waiting(replace(snapshot, admits=admitted), capabilities)

    def branched(self, slug: str, names: Iterable[str]) -> dict[str, dict[str, Any]]:
        """The tasks on any of the branches a continuation may take, cut to the fields its forbidden set reads."""
        wanted = sorted(set(names) - {""})
        if not wanted:
            return {}
        rows: list[dict[str, Any]] = self._ledger.tasks_where(slug, branches=wanted, fields=BRANCH_FIELDS)
        return {row["id"]: row for row in rows}

    def _related(self, slug: str, found: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        wanted = sorted(named(found) - {row["id"] for row in found})
        rows: list[dict[str, Any]] = self._ledger.tasks_where(slug, ids=wanted) if wanted else []
        return {row["id"]: row for row in rows}

    def _doc(self, slug: str, row: dict[str, Any] | None, rows: dict[str, Any]) -> dict[str, Any]:
        if row is not None and row.get("kind") == "plan":
            return self._full.state(slug)
        phase = self._ledger.item(slug, f"phases/{row['phase']}") if row and row.get("phase") else None
        overview = self._ledger.metadata(slug).get("overview", "")
        return {"overview": overview, "tasks": list(rows.values()), "phases": [phase] if phase else []}

    def _snapshot(self, slug: str, rows: dict[str, Any]) -> readiness.Snapshot:
        store = self._store()
        config = store.config(slug)
        doc = {"tasks": list(rows.values()), "phases": [], "freezes": []}
        fix = freeze.fix_phase(config)
        return readiness.Snapshot(
            slug, rows, doc, {}, fix, self._claims(store, slug), admits(store, slug, config, rows)
        )

    def _held(self, slug: str) -> Callable[[dict[str, Any]], str]:
        judged: dict[str, str] = {}

        def held(task: dict[str, Any]) -> str:
            if task["id"] not in judged:
                judged[task["id"]] = verdict(self._ledger.focus_verdicts(slug, f"tasks/{task['id']}"), task["id"])
            return judged[task["id"]]

        return held
