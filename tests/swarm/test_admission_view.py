"""The hub admission's narrow view: which rows it reads, the document its claim seeds read, the ledger's claim gate
verdict it trusts and the root slots it keeps."""

from collections.abc import Iterator
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import admission_view, readiness
from scripts.swarm.health import focus_check
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from tests.helpers.narrow_ledger import DocLedger
from tests.swarm.test_readiness import doc, task

SLUG = "sw"


class Recording(DocLedger):
    """A document ledger that records every narrow tasks selection and claim gate read it answers, and the swarm each
    read names."""

    def __init__(self, document: dict[str, Any]) -> None:
        super().__init__(document)
        self.selected: list[dict[str, Any]] = []
        self.judged: list[str] = []
        self.slugs: list[str] = []

    def task(self, slug: str, task_id: str) -> dict[str, Any] | None:
        self.slugs.append(slug)
        return super().task(slug, task_id)

    def item(self, slug: str, path: str) -> dict[str, Any] | None:
        self.slugs.append(slug)
        return super().item(slug, path)

    def metadata(self, slug: str) -> dict[str, Any]:
        self.slugs.append(slug)
        return super().metadata(slug)

    def state(self, slug: str) -> dict[str, Any]:
        self.slugs.append(slug)
        return super().state(slug)

    def tasks_where(self, slug: str, **filters: Any) -> list[dict[str, Any]]:
        self.slugs.append(slug)
        self.selected.append(filters)
        return super().tasks_where(slug, **filters)

    def focus_verdicts(self, slug: str, target: str) -> dict[str, Any] | None:
        self.slugs.append(slug)
        self.judged.append(target)
        return super().focus_verdicts(slug, target)


@pytest.fixture
def narrow() -> Iterator[SimpleNamespace]:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig(SLUG, "/repo", 2, 2))
    found = doc(
        [
            task("t1", depends_on=["t2"], parked_on=["t3"], group_members=["t4"], enhances="t5"),
            task("t2", state="done"),
            task("t3", state="done"),
            task("t4"),
            task("t5"),
            task("t6", state="claimed", territory=["scripts/hub"]),
            task("t7", state="pr", branch="t7-branch", pr_url="https://example.invalid/pull/7"),
            task("t8"),
        ]
    )
    ledger: Any = Recording(found)
    view = admission_view.Narrow(lambda: store, ledger, readiness.Source(store, ledger))
    yield SimpleNamespace(store=store, ledger=ledger, found=found, view=view)
    store.redis.close()


def marks() -> tuple[list[str], Any]:
    seen: list[str] = []
    return seen, seen.append


@pytest.mark.feature("hub-admission-narrow-read")
def test_the_view_reads_the_task_the_rows_it_names_and_the_running_rows_only(narrow: SimpleNamespace) -> None:
    seen, mark = marks()

    view = narrow.view.task(SLUG, "t1", mark)

    assert sorted(view.rows) == ["t1", "t2", "t3", "t4", "t5", "t6", "t7"]
    assert seen == ["row", "rows", "parts"]
    assert narrow.ledger.selected == [{"ids": ["t2", "t3", "t4", "t5"]}, {"states": ("claimed", "pr")}]
    assert narrow.ledger.exports == 0
    assert view.doc["overview"] == "o"
    assert [phase["id"] for phase in view.doc["phases"]] == ["p1"]
    assert {row["id"] for row in view.doc["tasks"]} == set(view.rows)


@pytest.mark.feature("hub-admission-narrow-read")
def test_a_task_with_nothing_named_reads_only_the_running_rows(narrow: SimpleNamespace) -> None:
    view = narrow.view.task(SLUG, "t8", marks()[1])

    assert sorted(view.rows) == ["t6", "t7", "t8"]
    assert narrow.ledger.selected == [{"states": ("claimed", "pr")}]


@pytest.mark.feature("hub-admission-narrow-read")
def test_a_gone_task_leaves_no_row_and_withdraws_its_offer(narrow: SimpleNamespace) -> None:
    view = narrow.view.task(SLUG, "missing", marks()[1])
    event = SimpleNamespace(key="missing", version=0, data={"lane": "eng"})

    assert "missing" not in view.rows
    assert view.doc["phases"] == []
    assert admission_view.withdrawal(event, view) == "the task is gone"
    assert narrow.ledger.judged == []


@pytest.mark.feature("hub-admission-narrow-read")
def test_a_plan_task_keeps_the_whole_document_for_its_steering(narrow: SimpleNamespace) -> None:
    narrow.found["tasks"].append(task("plan-p1", kind="plan", lane="plan"))

    view = narrow.view.task(SLUG, "plan-p1", marks()[1])

    assert view.doc["tasks"] == narrow.found["tasks"]
    assert narrow.ledger.exports == 1


@pytest.mark.feature("hub-admission-narrow-read")
def test_the_withdrawal_asks_the_ledger_claim_gate_once_the_local_checks_pass(narrow: SimpleNamespace) -> None:
    narrow.found["freezes"] = [{"verb": "focus", "target": "tasks/t4"}]
    view = narrow.view.task(SLUG, "t8", marks()[1])
    event = SimpleNamespace(key="t8", version=0, data={"lane": "eng"})

    assert admission_view.withdrawal(event, view) == "a focus elsewhere holds it"
    assert admission_view.withdrawal(event, view) == "a focus elsewhere holds it"
    assert narrow.ledger.judged == ["tasks/t8"]


@pytest.mark.feature("hub-admission-narrow-read")
def test_a_local_refusal_never_asks_the_ledger(narrow: SimpleNamespace) -> None:
    view = narrow.view.task(SLUG, "t8", marks()[1])
    event = SimpleNamespace(key="t8", version=3, data={"lane": "eng"})

    assert admission_view.withdrawal(event, view) == "it is at generation 0"
    assert narrow.ledger.judged == []


@pytest.mark.feature("hub-admission-narrow-read")
def test_the_view_counts_an_unfinished_agent_or_a_claimant_as_claimed(narrow: SimpleNamespace) -> None:
    record = AgentRecord(narrow.store.next_name(SLUG, "eng", 1), "eng", "t8", seat="eng-1@sw", started_at=1)
    narrow.store.start_execution(SLUG, record, "")
    narrow.store.claim(SLUG, "t4", "holder", 60_000)

    view = narrow.view.task(SLUG, "t1", marks()[1])

    assert [view.snapshot.claimed(task_id) for task_id in ("t8", "t4", "t1")] == [True, True, False]


@pytest.mark.parametrize(
    ("found", "expected"),
    [
        (None, "the task is gone"),
        ({"done": ["t1"], "tasks": []}, "it is done"),
        ({"done": [], "tasks": []}, "the task is gone"),
        ({"done": [], "tasks": [{"id": "t1", "verdict": focus_check.CLAIMABLE}]}, ""),
        (
            {"done": [], "tasks": [{"id": "t2", "verdict": "x"}, {"id": "t1", "verdict": "it waits on task t2"}]},
            "it waits on task t2",
        ),
    ],
    ids=["missing", "done", "absent", "claimable", "held"],
)
def test_the_verdict_names_the_ledgers_hold_reason(found: dict[str, Any] | None, expected: str) -> None:
    assert admission_view.verdict(found, "t1") == expected


def test_named_collects_every_listed_and_linked_id() -> None:
    rows = [
        {"id": "a", "depends_on": ["b"], "parked_on": ["c"], "group_members": ["d"], "enhances": "e"},
        {"id": "f", "merged_into": "g", "depends_on": None},
    ]

    assert admission_view.named(rows) == {"b", "c", "d", "e", "g"}


@pytest.mark.feature("hub-admission-narrow-read")
def test_the_root_waiting_count_judges_only_open_tasks_pinned_to_the_root_hive(narrow: SimpleNamespace) -> None:
    narrow.found["tasks"] += [
        task("w1", workstation=True),
        task("w2", hive="root", depends_on=["t8"]),
        task("w3", hive="hive-b"),
        task("w4", workstation=True, state="claimed"),
    ]

    assert narrow.view.waiting(SLUG, lambda: []) == 1
    assert narrow.ledger.selected == [{"states": ("open",), "pinned": True}, {"ids": ["t8"]}]
    assert narrow.ledger.judged == ["tasks/w1"]
    assert narrow.ledger.exports == 0


def test_a_swarm_with_no_pinned_task_keeps_no_root_slot(narrow: SimpleNamespace) -> None:
    assert narrow.view.waiting(SLUG, lambda: []) == 0
    assert narrow.ledger.judged == []


@pytest.mark.feature("hub-admission-narrow-read")
def test_the_branched_rows_carry_only_the_continuation_fields(narrow: SimpleNamespace) -> None:
    assert narrow.view.branched(SLUG, ["t7-branch", "", "gone"]) == {
        "t7": {"id": "t7", "branch": "t7-branch", "pr_url": "https://example.invalid/pull/7"}
    }
    assert narrow.ledger.selected == [{"branches": ["gone", "t7-branch"], "fields": admission_view.BRANCH_FIELDS}]


def test_a_continuation_with_no_branch_reads_no_rows(narrow: SimpleNamespace) -> None:
    assert narrow.view.branched(SLUG, ["", ""]) == {}
    assert narrow.ledger.selected == []


@pytest.mark.feature("hub-admission-narrow-read")
def test_a_frozen_root_task_keeps_no_root_slot(narrow: SimpleNamespace) -> None:
    narrow.found["tasks"].append(task("w1", workstation=True))
    narrow.found["freezes"] = [{"verb": "freeze", "target": "phases/p1"}]

    assert narrow.view.waiting(SLUG, lambda: []) == 0
    assert narrow.ledger.judged == ["tasks/w1"]


@pytest.mark.feature("hub-admission-narrow-read")
def test_every_read_the_view_makes_names_its_own_swarm(narrow: SimpleNamespace) -> None:
    narrow.found["tasks"] += [task("plan-p1", kind="plan", lane="plan"), task("w2", hive="root", depends_on=["t8"])]

    narrow.view.task(SLUG, "t1", marks()[1])
    narrow.view.task(SLUG, "plan-p1", marks()[1])
    narrow.view.waiting(SLUG, lambda: [])
    narrow.view.branched(SLUG, ["t7-branch"])

    assert narrow.ledger.slugs
    assert set(narrow.ledger.slugs) == {SLUG}


@pytest.mark.feature("hub-admission-narrow-read")
def test_the_snapshot_holds_the_rows_the_view_read_under_its_swarm_and_fix_phase(narrow: SimpleNamespace) -> None:
    judged: list[tuple[str, list[str]]] = []

    def admitting(store: RedisStore, slug: str, config: SwarmConfig, rows: dict[str, Any]) -> Any:
        judged.append((slug, sorted(rows)))
        return lambda task: True

    view = admission_view.Narrow(
        lambda: narrow.store, narrow.ledger, readiness.Source(narrow.store, narrow.ledger), admitting=admitting
    ).task(SLUG, "t1", marks()[1])

    assert view.snapshot.slug == SLUG
    assert view.snapshot.fix == ""
    assert view.snapshot.doc == {"tasks": list(view.rows.values()), "phases": [], "freezes": []}
    assert judged == [(SLUG, sorted(view.rows))]


def test_a_ledger_without_an_overview_gives_the_view_an_empty_one(
    narrow: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(narrow.ledger, "metadata", lambda slug: {"_meta": {"rev": 1}})

    assert narrow.view.task(SLUG, "t1", marks()[1]).doc["overview"] == ""


def test_a_finished_agent_leaves_its_task_unclaimed(narrow: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    agents = [SimpleNamespace(task="t5", state="finished"), SimpleNamespace(task="t6", state="working")]
    monkeypatch.setattr(narrow.store, "agents", lambda slug: agents)

    claimed = admission_view.claimed(narrow.store, SLUG)

    assert [claimed(task_id) for task_id in ("t5", "t6")] == [False, True]


@pytest.mark.feature("hub-admission-narrow-read")
@pytest.mark.parametrize("denied", ["", "state", "released", "applied", "unshared", "lab"])
def test_the_admits_gate_needs_a_running_swarm_and_every_wait_clear(
    narrow: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, denied: str
) -> None:
    seen: list[tuple[str, Any]] = []

    def gate(name: str) -> Any:
        def check(task: dict[str, Any]) -> bool:
            seen.append((name, task["id"]))
            return denied != name

        return check

    def released(config: SwarmConfig) -> Any:
        seen.append(("release config", config.slug))
        return gate("released")

    def applied(config: SwarmConfig, rows: dict[str, Any]) -> Any:
        seen.append(("apply rows", sorted(rows)))
        return gate("applied")

    def lab(store: RedisStore, slug: str, fix: str) -> Any:
        seen.append(("lab fix", fix))

        def reason(task: dict[str, Any]) -> str:
            seen.append(("lab", task["id"]))
            return "held by the lab" if denied == "lab" else ""

        return reason

    monkeypatch.setattr(admission_view.release_wait, "checker", released)
    monkeypatch.setattr(admission_view.apply_wait, "checker", applied)
    monkeypatch.setattr(admission_view.fleet_red, "checker", lambda reader: gate("unshared"))
    monkeypatch.setattr(admission_view.claim_hold, "lab_reason", lab)
    config = replace(narrow.store.config(SLUG), state="paused" if denied == "state" else "running")

    admits = admission_view.admits(narrow.store, SLUG, config, {"t1": {"id": "t1"}})

    assert admits({"id": "t1"}) is (denied == "")
    assert ("release config", SLUG) in seen
    assert ("apply rows", ["t1"]) in seen
    assert ("lab fix", "") in seen
