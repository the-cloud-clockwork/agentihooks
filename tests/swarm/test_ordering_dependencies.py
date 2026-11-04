import json

import pytest

from scripts.swarm import status
from scripts.swarm.health import ordering_dependencies
from scripts.swarm.store import RedisStore, SwarmConfig

pytestmark = pytest.mark.unit

SLUG = "sw"


@pytest.fixture
def store():
    import fakeredis

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig(SLUG, "/repo", max_eng=0, max_ci=0))
    return saved


def doc():
    return {
        "tasks": [
            {"id": "a", "title": "status list", "state": "open", "territory": ["scripts/swarm/status.py"]},
            {
                "id": "b",
                "title": "hive beat",
                "state": "open",
                "territory": ["scripts/hive/daemon.py"],
                "depends_on": ["a"],
                "description": "beat faster",
            },
            {
                "id": "c",
                "title": "status column",
                "state": "open",
                "territory": ["docs/"],
                "depends_on": ["a"],
                "description": "read scripts/swarm/status.py",
            },
        ]
    }


ROW = {"task": "b", "title": "hive beat", "depends_on": "a", "dependency_title": "status list"}


def test_the_tick_records_each_ordering_only_dependency(store):
    assert ordering_dependencies.observe(store, SLUG, doc()) == [ROW]
    assert json.loads(store.redis.get(store.key(SLUG, "dependency-audit"))) == [ROW]
    assert ordering_dependencies.read(store, SLUG) == [ROW]


def test_a_later_pass_replaces_the_recorded_list(store):
    ordering_dependencies.observe(store, SLUG, doc())
    ordering_dependencies.observe(store, SLUG, {"tasks": []})
    assert ordering_dependencies.read(store, SLUG) == []


def test_a_ledger_without_tasks_records_an_empty_list(store):
    assert ordering_dependencies.observe(store, SLUG, {}) == []
    assert ordering_dependencies.read(store, SLUG) == []


def test_the_plan_lines_are_read_from_the_ledger_being_audited(store, monkeypatch):
    from scripts.swarm_ledger import dependency_audit

    monkeypatch.setattr(dependency_audit.plan_read, "exact", lambda ledger, row: ledger["plan"])
    tasks = doc()["tasks"]
    tasks[1]["plan_lines"] = "1-2"
    ledger: dict[str, object] = {"tasks": tasks, "plan": "beat faster still"}
    assert ordering_dependencies.observe(store, SLUG, ledger) == [ROW]


def test_nothing_recorded_reads_as_an_empty_list(store):
    assert ordering_dependencies.read(store, SLUG) == []
    assert ordering_dependencies.findings(store, SLUG) == []


def test_each_ordering_only_dependency_raises_a_health_finding(store):
    ordering_dependencies.observe(store, SLUG, doc())
    [found] = ordering_dependencies.findings(store, SLUG)
    assert found.id == "ordering-only-dependency/b-after-a"
    assert found.kind == "ordering only dependency"
    assert found.subject == "b-after-a"
    assert found.summary == (
        "task hive beat depends on status list, but the two share no code and its plan names none of that code"
    )
    assert found.evidence == (
        "their territories share no code file",
        "the dependent plan lines name no code of the task it waits on",
    )
    assert found.threshold == "a dependency with no shared or named code"
    assert found.measure == 1
    assert found.evidence_identity == ("b", "a")


def test_status_prints_the_list(store):
    ordering_dependencies.observe(store, SLUG, doc())
    assert status.status_report(store, SLUG, {"tasks": []}, found=[])["ordering_dependencies"] == [ROW]


OTHER_FINDINGS = (
    "live_binding",
    "retire_watch",
    "stall_watch",
    "drain_watch",
    "launch_check",
    "spawn_stall",
    "night_watch",
    "merge_stall",
    "prompt_backlog",
    "urgent_hold",
    "placement_waits",
    "standing",
    "early_ends",
)


def test_status_raises_a_finding_per_ordering_only_dependency(store, monkeypatch):
    seen = []

    def ledger_findings(ledger, *rest):
        seen.append(("health", ledger["tasks"]))
        return []

    def none(first, slug, *rest):
        assert first in (store, store.redis)
        seen.append(slug)
        return []

    monkeypatch.setattr(status.health, "findings", ledger_findings)
    for name in OTHER_FINDINGS:
        monkeypatch.setattr(f"scripts.swarm.status.{name}.findings", none)
    ordering_dependencies.observe(store, SLUG, doc())
    found = status.findings(store, SLUG, store.config(SLUG), [], [])
    assert [(row["id"], row["kind"]) for row in found] == [
        ("ordering-only-dependency/b-after-a", "ordering only dependency")
    ]
    assert seen == [("health", []), *[SLUG] * len(OTHER_FINDINGS)]
