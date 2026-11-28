"""The hub admission's narrow view: which rows it reads, the document its claim seeds read, the ledger's claim gate
verdict it trusts and the root slots it keeps."""

import time
from collections.abc import Iterable, Iterator
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import admission_view, claim_hold, lab_capacity
from scripts.swarm.health import focus_check
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_ledger.api import resources
from tests.helpers.narrow_ledger import DocLedger
from tests.swarm.test_readiness import doc, task

SLUG = "sw"
RUNNING_FIELDS = ("state", "claimed_by", "awaiting", "done", "territory", "branch", "branch_repo", "pr_url")
WAITING_FIELDS = (
    "after_release",
    "applied_at",
    "awaiting",
    "branch",
    "claimed_by",
    "contract",
    "depends_on",
    "enhancement",
    "enhances",
    "frozen",
    "group_members",
    "hive",
    "kind",
    "lane",
    "merged_into",
    "needs",
    "out_of_scope",
    "parked_on",
    "phase",
    "pr_url",
    "rank",
    "slice",
    "state",
    "workstation",
)
RELATED_FIELDS = (
    "after_release",
    "applied_at",
    "awaiting",
    "branch",
    "branch_repo",
    "claimed_by",
    "contract",
    "depends_on",
    "description",
    "done",
    "enhancement",
    "enhances",
    "frozen",
    "group_members",
    "hive",
    "kind",
    "lane",
    "merged_into",
    "needs",
    "out_of_scope",
    "parked_on",
    "phase",
    "pr_url",
    "rank",
    "slice",
    "state",
    "territory",
    "title",
    "workstation",
)
RUNNING_ROW = {
    "state": "claimed",
    "claimed_by": "engineer@h-1",
    "awaiting": "checks",
    "done": False,
    "territory": ["scripts/hub"],
    "branch": "c1-branch",
    "branch_repo": "org/repo",
    "pr_url": "https://example.invalid/pull/9",
}


class Recording(DocLedger):
    """A document ledger that records every narrow tasks selection and claim gate read it answers, and the swarm each
    read names."""

    def __init__(self, document: dict[str, Any]) -> None:
        super().__init__(document)
        self.selected: list[dict[str, Any]] = []
        self.judged: list[str] = []
        self.slugs: list[str] = []
        self.reads: list[str] = []

    def task(self, slug: str, task_id: str) -> dict[str, Any] | None:
        self.slugs.append(slug)
        return super().task(slug, task_id)

    def item(self, slug: str, path: str) -> dict[str, Any] | None:
        self.slugs.append(slug)
        self.reads.append(path)
        return super().item(slug, path)

    def thread(self, slug: str, record: str, name: str) -> list[dict[str, Any]]:
        self.slugs.append(slug)
        self.reads.append(f"{record}/{name}")
        return super().thread(slug, record, name)

    def tasks_in_phases(self, slug: str, phases: Iterable[str], fields: Iterable[str] = ()) -> list[dict[str, Any]]:
        self.slugs.append(slug)
        self.reads.append(f"tasks of {','.join(phases)}")
        return super().tasks_in_phases(slug, phases, fields)

    def artifacts(self, slug: str) -> list[dict[str, Any]]:
        self.slugs.append(slug)
        self.reads.append("artifacts")
        return super().artifacts(slug)

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
    view = admission_view.Narrow(lambda: store, ledger)
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
    assert narrow.ledger.selected == [
        {"ids": ["t2", "t3", "t4", "t5"], "fields": RELATED_FIELDS},
        {"states": ("claimed", "pr"), "fields": RUNNING_FIELDS},
    ]
    assert narrow.ledger.exports == 0
    assert view.doc["overview"] == "o"
    assert [phase["id"] for phase in view.doc["phases"]] == ["p1"]
    assert {row["id"] for row in view.doc["tasks"]} == set(view.rows)


@pytest.mark.feature("hub-admission-narrow-read")
def test_a_task_with_nothing_named_reads_only_the_running_rows(narrow: SimpleNamespace) -> None:
    view = narrow.view.task(SLUG, "t8", marks()[1])

    assert sorted(view.rows) == ["t6", "t7", "t8"]
    assert narrow.ledger.selected == [{"states": ("claimed", "pr"), "fields": RUNNING_FIELDS}]


def oversized(task_id: str, **fields: Any) -> dict[str, Any]:
    """A task row whose description alone takes half the ledger's reply cap."""
    return task(task_id, description="x" * (resources.MAX_REPLY // 2), **fields)


@pytest.mark.feature("hub-admission-running-fields")
def test_running_rows_over_the_reply_cap_still_give_the_admission_its_view(narrow: SimpleNamespace) -> None:
    narrow.found["tasks"] += [oversized("c1", kind="ops", applied_at=5, **RUNNING_ROW), oversized("c2", state="pr")]

    view = narrow.view.task(SLUG, "t8", marks()[1])

    assert sorted(view.rows) == ["c1", "c2", "t6", "t7", "t8"]
    assert view.rows["c1"] == {"id": "c1", **RUNNING_ROW}
    assert view.rows["c2"] == {"id": "c2", "state": "pr"}


@pytest.mark.feature("hub-admission-running-fields")
def test_a_running_group_member_keeps_what_its_readers_take(narrow: SimpleNamespace) -> None:
    narrow.found["tasks"] += [
        task("g0", group_members=["g1"]),
        task("g1", state="claimed", description="member work", kind="ops", applied_at=7, territory=["scripts/hub"]),
    ]

    row = narrow.view.task(SLUG, "g0", marks()[1]).rows["g1"]

    assert {key: row[key] for key in ("title", "description", "phase", "kind", "applied_at", "territory")} == {
        "title": "title g1",
        "description": "member work",
        "phase": "p1",
        "kind": "ops",
        "applied_at": 7,
        "territory": ["scripts/hub"],
    }


@pytest.mark.feature("hub-admission-narrow-read")
def test_a_gone_task_leaves_no_row_and_withdraws_its_offer(narrow: SimpleNamespace) -> None:
    view = narrow.view.task(SLUG, "missing", marks()[1])
    event = SimpleNamespace(key="missing", version=0, data={"lane": "eng"})

    assert "missing" not in view.rows
    assert view.doc["phases"] == []
    assert admission_view.withdrawal(event, view) == "the task is gone"
    assert narrow.ledger.judged == []


@pytest.mark.feature("hub-admission-narrow-read")
def test_a_plan_task_reads_its_phase_without_the_whole_document(narrow: SimpleNamespace) -> None:
    narrow.found["tasks"].append(task("plan-p1", kind="plan", lane="plan"))

    view = narrow.view.task(SLUG, "plan-p1", marks()[1])

    assert {row["id"] for row in view.doc["tasks"]} == set(view.rows)
    assert [phase["id"] for phase in view.doc["phases"]] == ["p1"]
    assert "artifacts" not in view.doc
    assert narrow.ledger.reads == ["phases/p1"]
    assert narrow.ledger.exports == 0


PLAN_TEXT = "# Plan\n\n## Build\n\n<!-- slice: one -->\n### one\n- First slice\n"


def comments(*texts: str, deleted: int = -1) -> list[dict[str, Any]]:
    return [
        {"id": f"c{n}", "by": "engineer@h-1", "at": n, "text": text, **({"deleted": True} if n == deleted else {})}
        for n, text in enumerate(texts)
    ]


def planned(found: dict[str, Any]) -> None:
    """Phase p4 holds a plan task enhancing t5 and a published plan; it depends on p3 and p2, p3 on p2 and back on
    p4, p2 on p1 twice and on itself; dependency phases carry comments, one deleted, and tasks with proofs and pull
    requests."""
    phases = {phase["id"]: phase for phase in found["phases"]}
    phases["p1"]["comments"] = comments("p1 started")
    phases["p2"].update(
        depends_on=["p1", "p1", "p2"], comments=comments(*(f"p2 note {n}" for n in range(7)), deleted=6)
    )
    found["phases"] += [
        {"id": "p3", "title": "wire", "depends_on": ["p2", "p4"], "comments": comments("wired")},
        {
            "id": "p4",
            "title": "plan",
            "description": "plan the rest",
            "depends_on": ["p3", "p2"],
            "comments": comments("own phase talk"),
            "review": {"state": "sent_back", "note": "split it"},
            "plan_ref": {"artifact": f"https://ledger.example/artifacts/{SLUG}/plan-file", "lines": "3-7"},
        },
    ]
    found["artifacts"] = [
        {"id": "artifact_add-a1", "title": "plan", "plan": True, "file": {"id": "plan-file"}},
        {"id": "artifact_add-a2", "title": "logo", "file": {"id": "logo-file"}},
    ]
    rows = {row["id"]: row for row in found["tasks"]}
    rows["t5"].update(description="target work", territory=["scripts/x"])
    rows["t6"].update(claimed_by="engineer@h-1", proof={"evidence": "run 9"})
    found["tasks"] += [
        task("d3", phase="p3", state="pr", pr_url="https://example.invalid/pull/31"),
        task("d2", phase="p2", state="done", proof={"evidence": "run 2"}),
        task("plan-p4", phase="p4", kind="plan", lane="plan", enhances="t5"),
    ]


@pytest.mark.feature("plan-lane-admission-narrow-read")
def test_a_plan_task_steering_from_the_view_equals_the_steering_from_the_whole_export(
    narrow: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm_ledger import ledger_artifacts, ledger_workspace

    monkeypatch.delenv("AGENTIHOOKS_PLAN_EVIDENCE_CHARS", raising=False)
    monkeypatch.setattr(ledger_artifacts, "read", lambda slug, file_id: PLAN_TEXT.encode())
    planned(narrow.found)

    view = narrow.view.task(SLUG, "plan-p4", marks()[1])
    row = view.rows["plan-p4"]
    steering = ledger_workspace.steering(row, view.doc)

    assert steering == ledger_workspace.steering(row, narrow.found)
    for part in (
        "Dependency tasks for wire",
        "Last five comments for later\np2 note 1",
        "Dependency tasks for build",
        "run 9",
        "Review note\nsplit it",
        "Slice names\nThe phase plan is already published",
        "target work",
    ):
        assert part in steering


@pytest.mark.feature("plan-lane-admission-narrow-read")
def test_a_plan_task_reads_each_dependency_phase_and_its_comments_once_and_never_the_export(
    narrow: SimpleNamespace,
) -> None:
    planned(narrow.found)

    view = narrow.view.task(SLUG, "plan-p4", marks()[1])

    assert narrow.ledger.exports == 0
    assert narrow.ledger.reads == [
        "phases/p4",
        "phases/p3",
        "phases/p3/comments",
        "phases/p2",
        "phases/p2/comments",
        "phases/p1",
        "phases/p1/comments",
        "tasks of p3,p2,p1",
        "artifacts",
    ]
    assert set(narrow.ledger.slugs) == {SLUG}
    assert [phase["id"] for phase in view.doc["phases"]] == ["p4", "p3", "p2", "p1"]
    assert [entry["text"] for entry in view.doc["phases"][1]["comments"]] == ["wired"]
    assert [row["id"] for row in view.doc["artifacts"]] == ["artifact_add-a1", "artifact_add-a2"]


@pytest.mark.feature("plan-lane-admission-narrow-read")
def test_a_dependency_task_the_view_already_holds_keeps_its_fields_under_the_evidence_fields(
    narrow: SimpleNamespace,
) -> None:
    planned(narrow.found)

    listed = narrow.view.task(SLUG, "plan-p4", marks()[1]).doc["tasks"]
    tasks = {row["id"]: row for row in listed}

    assert len(tasks) == len(listed)
    assert [key for key in tasks if key.startswith("d")] == ["d3", "d2"]
    assert tasks["d3"] == {
        "id": "d3",
        "phase": "p3",
        "pr_url": "https://example.invalid/pull/31",
        "state": "pr",
        "title": "title d3",
    }
    assert tasks["t5"]["description"] == "target work" and tasks["t5"]["territory"] == ["scripts/x"]
    assert tasks["t6"]["claimed_by"] == "engineer@h-1" and tasks["t6"]["proof"] == {"evidence": "run 9"}


@pytest.mark.feature("plan-lane-admission-narrow-read")
def test_a_dependency_phase_the_ledger_no_longer_holds_ends_the_walk_as_the_evidence_does(
    narrow: SimpleNamespace,
) -> None:
    from scripts.swarm_ledger import ledger_workspace

    narrow.found["phases"].append({"id": "p5", "title": "orphan", "depends_on": ["gone", "p1"]})
    narrow.found["tasks"].append(task("plan-p5", phase="p5", kind="plan", lane="plan"))

    view = narrow.view.task(SLUG, "plan-p5", marks()[1])
    row = view.rows["plan-p5"]

    assert narrow.ledger.reads == ["phases/p5", "phases/gone"]
    assert narrow.ledger.exports == 0
    with pytest.raises(KeyError):
        ledger_workspace.steering(row, narrow.found)
    with pytest.raises(KeyError):
        ledger_workspace.steering(row, view.doc)


@pytest.mark.feature("plan-lane-admission-narrow-read")
@pytest.mark.parametrize(("phase", "reads"), [("", []), ("unknown", ["phases/unknown"])], ids=["no-phase", "unknown"])
def test_a_plan_task_without_a_phase_gets_only_the_project_intent_without_the_export(
    narrow: SimpleNamespace, phase: str, reads: list[str]
) -> None:
    from scripts.swarm_ledger import ledger_workspace

    narrow.found["tasks"].append(task("plan-x", phase=phase, kind="plan", lane="plan"))

    view = narrow.view.task(SLUG, "plan-x", marks()[1])
    row = view.rows["plan-x"]

    assert narrow.ledger.reads == reads
    assert narrow.ledger.exports == 0
    assert ledger_workspace.steering(row, view.doc) == ledger_workspace.steering(row, narrow.found)


@pytest.mark.feature("plan-lane-admission-narrow-read")
def test_a_task_of_another_kind_in_a_phase_with_dependencies_reads_only_its_phase(narrow: SimpleNamespace) -> None:
    planned(narrow.found)
    narrow.found["tasks"].append(task("e4", phase="p4", kind="ops"))

    view = narrow.view.task(SLUG, "e4", marks()[1])

    assert narrow.ledger.reads == ["phases/p4"]
    assert [phase["id"] for phase in view.doc["phases"]] == ["p4"]
    assert "artifacts" not in view.doc


@pytest.mark.feature("plan-lane-admission-narrow-read")
@pytest.mark.parametrize(
    ("planned_task", "reads"),
    [
        ("plan-p6", ["phases/p6"]),
        ("plan-p7", ["phases/p7", "phases/p6", "phases/p6/comments", "tasks of p6"]),
    ],
    ids=["own-phase", "dependency"],
)
def test_a_phase_that_names_no_dependency_list_ends_the_walk_there(
    narrow: SimpleNamespace, planned_task: str, reads: list[str]
) -> None:
    from scripts.swarm_ledger import ledger_workspace

    narrow.found["phases"] += [{"id": "p6", "title": "alone"}, {"id": "p7", "title": "after", "depends_on": ["p6"]}]
    narrow.found["tasks"] += [
        task("plan-p6", phase="p6", kind="plan", lane="plan"),
        task("plan-p7", phase="p7", kind="plan", lane="plan"),
    ]

    view = narrow.view.task(SLUG, planned_task, marks()[1])
    row = view.rows[planned_task]

    assert narrow.ledger.reads == reads
    assert ledger_workspace.steering(row, view.doc) == ledger_workspace.steering(row, narrow.found)


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
    assert narrow.ledger.selected == [
        {"states": ("open",), "pinned": True, "fields": WAITING_FIELDS},
        {"ids": ["t8"], "fields": RELATED_FIELDS},
    ]
    assert narrow.ledger.judged == ["tasks/w1"]
    assert narrow.ledger.exports == 0


@pytest.mark.feature("hub-admission-running-fields")
def test_pinned_open_rows_over_the_reply_cap_keep_the_same_root_waiting_count(narrow: SimpleNamespace) -> None:
    narrow.found["tasks"] += [
        oversized("w1", workstation=True),
        oversized("w2", hive="root", depends_on=["t8"]),
        oversized("w3", hive="hive-b"),
    ]

    assert narrow.view.waiting(SLUG, lambda: []) == 1
    assert narrow.ledger.judged == ["tasks/w1"]


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

    view = admission_view.Narrow(lambda: narrow.store, narrow.ledger, admitting=admitting).task(SLUG, "t1", marks()[1])

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


def lab_behind(store: RedisStore, slug: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIHOOKS_HEALTH_CI_BEHIND_MINUTES", "10")
    capacity = {
        "classes": {claim_hold.LAB_CLASS: {"pending": 4, "running": 2, "predicted_wait_s": 900}},
        "slots": {"total": 2, "busy": 2},
        "shed": [],
        "utilisation": 1.0,
        "budget_fraction": 1.0,
    }
    monkeypatch.setattr(lab_capacity, "read", lambda url: capacity)
    assert lab_capacity.capture(store, slug, "http://lab.invalid", int(time.time() * 1000))


@pytest.mark.feature("hub-admission-composed-admits-gate")
def test_the_composed_admits_gate_holds_eng_behind_a_slow_lab_and_refuses_every_lane_while_paused(
    narrow: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng, ci = task("t1"), task("t2", lane="ci")
    rows = {row["id"]: row for row in (eng, ci)}
    running = replace(narrow.store.config(SLUG), state="running")
    clear = admission_view.admits(narrow.store, SLUG, running, rows)
    lab_behind(narrow.store, SLUG, monkeypatch)

    behind = admission_view.admits(narrow.store, SLUG, running, rows)
    paused = admission_view.admits(narrow.store, SLUG, replace(running, state="paused"), rows)

    assert (clear(eng), clear(ci)) == (True, True)
    assert (behind(eng), behind(ci)) == (False, True)
    assert (paused(eng), paused(ci)) == (False, False)
