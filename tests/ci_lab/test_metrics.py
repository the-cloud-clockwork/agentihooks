import re
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.ci_lab import metrics
from scripts.ci_lab.capacity import Load
from scripts.ci_lab.delivery import CLASSES
from scripts.swarm import metrics_outbox

CONTRACT_NAMES = (
    "ci_lab_units_queued",
    "ci_lab_units_running",
    "ci_lab_slots_total",
    "ci_lab_slots_busy",
    "ci_lab_slots_wanted",
    "ci_lab_queue_wait_seconds",
    "ci_lab_unit_duration_seconds",
    "ci_lab_cache_hits_total",
    "ci_lab_cache_misses_total",
    "ci_lab_mirror_age_seconds",
    "ci_lab_core_seconds_total",
    "ci_lab_heads_graded_total",
    "ci_lab_budget_fraction",
    "ci_lab_class_shed",
    "ci_lab_units_redelivered_total",
    "ci_lab_units_lost_total",
    "ci_lab_units_overdue",
    "ci_lab_workers_ready",
    "ci_lab_github_rate_remaining",
    "ci_lab_verdict_mismatch_total",
)
COUNTERS = {
    "ci_lab_cache_hits_total",
    "ci_lab_cache_misses_total",
    "ci_lab_core_seconds_total",
    "ci_lab_heads_graded_total",
    "ci_lab_units_redelivered_total",
    "ci_lab_units_lost_total",
    "ci_lab_verdict_mismatch_total",
}
VALUE = r'"(?:[^"\\]|\\.)*"'
SAMPLE = re.compile(rf"^([a-z_]+)(\{{[a-z_]+={VALUE}(,[a-z_]+={VALUE})*\}})? (\+Inf|-?[0-9.e+-]+)$")


def test_the_contract_holds_exactly_the_architecture_names() -> None:
    assert tuple(metric.name for metric in metrics.CONTRACT) == CONTRACT_NAMES


def test_every_contract_metric_is_emitted() -> None:
    text = metrics.Registry().render()
    for name in CONTRACT_NAMES:
        kind = "counter" if name in COUNTERS else "gauge"
        assert f"# TYPE {name} {kind}\n" in text
        assert f"# HELP {name} " in text
    for metric in metrics.CONTRACT:
        if not metric.labels:
            assert f"\n{metric.name} 0.0\n" in text


def test_every_sample_line_is_valid_exposition() -> None:
    registry = metrics.Registry()
    load = Load({"hotfix": (30.0,)}, {"hotfix": 1}, slots_total=1, slots_busy=1, budget_fraction=0.5)
    metrics.observe_load(registry, load, {"hotfix": None, "queue": 12.5})
    registry.inc("ci_lab_heads_graded_total", 1, {"class": "pull_request", "conclusion": "success"})
    registry.inc("ci_lab_verdict_mismatch_total", 2, {"gate": 'tests "unit"\\x'})
    text = registry.render()
    assert text.endswith("\n")
    for line in text.splitlines():
        if not line.startswith("#"):
            assert SAMPLE.match(line), line
    assert 'ci_lab_queue_wait_seconds{class="hotfix"} +Inf' in text
    assert 'ci_lab_queue_wait_seconds{class="queue"} 12.5' in text
    assert 'ci_lab_verdict_mismatch_total{gate="tests \\"unit\\"\\\\x"} 2.0' in text


def test_observe_load_sets_queue_slot_budget_and_shed_gauges() -> None:
    registry = metrics.Registry()
    load = Load(
        pending={"pull_request": (10.0, 20.0)},
        running={"queue": 2},
        slots_total=5,
        slots_busy=2,
        budget_fraction=0.25,
        shed=("backfill",),
    )
    metrics.observe_load(registry, load, {name: 0.0 for name in CLASSES})
    text = registry.render()
    for name in CLASSES:
        assert f'ci_lab_units_queued{{class="{name}"}}' in text
    assert 'ci_lab_units_queued{class="pull_request"} 2.0' in text
    assert 'ci_lab_units_running{class="queue"} 2.0' in text
    assert 'ci_lab_class_shed{class="backfill"} 1.0' in text
    assert 'ci_lab_class_shed{class="queue"} 0.0' in text
    assert "\nci_lab_slots_total 5.0\n" in text
    assert "\nci_lab_slots_busy 2.0\n" in text
    assert "\nci_lab_budget_fraction 0.25\n" in text


def test_counters_accumulate_and_gauges_replace() -> None:
    registry = metrics.Registry()
    registry.inc("ci_lab_units_lost_total")
    registry.inc("ci_lab_units_lost_total", 2)
    registry.set("ci_lab_workers_ready", 3)
    registry.set("ci_lab_workers_ready", 1)
    text = registry.render()
    assert "\nci_lab_units_lost_total 3.0\n" in text
    assert "\nci_lab_workers_ready 1.0\n" in text


@pytest.mark.parametrize(
    ("call", "name", "labels"),
    [
        ("set", "ci_lab_units_lost_total", None),
        ("inc", "ci_lab_workers_ready", None),
        ("set", "ci_lab_units_queued", None),
        ("set", "ci_lab_units_queued", {"class": "queue", "kind": "x"}),
        ("inc", "ci_lab_heads_graded_total", {"class": "queue"}),
    ],
)
def test_wrong_type_or_labels_are_refused(call: str, name: str, labels: dict[str, str] | None) -> None:
    registry = metrics.Registry()
    with pytest.raises(ValueError, match=name):
        getattr(registry, call)(name, 1.0, labels)


def test_a_negative_counter_increment_is_refused() -> None:
    with pytest.raises(ValueError, match="non negative"):
        metrics.Registry().inc("ci_lab_units_lost_total", -1)


def test_lab_tables_are_declared_for_the_outbox() -> None:
    assert metrics.UNITS.name == "lab_units"
    assert metrics.GRADES.name == "lab_grades"
    assert "CREATE TABLE IF NOT EXISTS swarm.lab_units" in metrics.UNITS.ddl()
    assert "CREATE TABLE IF NOT EXISTS swarm.lab_grades" in metrics.GRADES.ddl()


def unit(head: str = "a" * 40, key: str = "k1") -> metrics.UnitRecord:
    return metrics.UnitRecord(
        repo="org/repo",
        head=head,
        unit_key=key,
        kind="tests",
        priority="pull_request",
        worker="worker-0",
        conclusion="success",
        cached=True,
        seconds=12,
        wait_s=3,
        ts_ms=1_700_000_000_000,
    )


def grade() -> metrics.GradeRecord:
    return metrics.GradeRecord(
        repo="org/repo",
        head="b" * 40,
        base="c" * 40,
        priority="queue",
        conclusion="failure",
        units=7,
        cached_units=4,
        seconds=310,
        ts_ms=1_700_000_000_001,
    )


def test_unit_and_grade_rows_pass_the_outbox_schema() -> None:
    row = metrics.unit_row(unit())
    metrics.UNITS.check(row)
    assert row["class"] == "pull_request"
    assert row["cached"] == 1
    assert row["seconds"] == 12.0
    assert row["ledger"] == metrics.LEDGER
    graded = metrics.grade_row(grade())
    metrics.GRADES.check(graded)
    assert graded["class"] == "queue"
    assert graded["units"] == 7
    assert graded["cached_units"] == 4


def test_unit_rows_are_keyed_by_repo_head_and_unit() -> None:
    first = metrics.unit_row(unit())["event_id"]
    assert metrics.unit_row(unit())["event_id"] == first
    assert metrics.unit_row(unit(head="d" * 40))["event_id"] != first
    assert metrics.unit_row(unit(key="k2"))["event_id"] != first


class Spool:
    def __init__(self) -> None:
        self.appended: list[tuple[str, list[dict[str, object]]]] = []

    def append(self, table: metrics_outbox.Table, rows: list[dict[str, object]]) -> None:
        for row in rows:
            table.check(row)
        self.appended.append((table.name, rows))


def test_record_spools_units_then_grades_and_skips_empty_tables() -> None:
    spool = Spool()
    metrics.record(spool, [unit(), unit(key="k2")], [grade()])
    assert [(name, len(rows)) for name, rows in spool.appended] == [("lab_units", 2), ("lab_grades", 1)]
    empty = Spool()
    metrics.record(empty, [], [])
    assert empty.appended == []


def test_observe_records_counts_cache_use_durations_and_graded_heads() -> None:
    registry = metrics.Registry()
    missed = replace(unit(), cached=False, kind="mutation", seconds=40.0)
    metrics.observe_records(registry, [unit(), unit(key="k2"), missed], [grade(), grade()])
    text = registry.render()
    assert 'ci_lab_cache_hits_total{cache="verdict"} 2.0' in text
    assert 'ci_lab_cache_misses_total{cache="verdict"} 1.0' in text
    assert 'ci_lab_unit_duration_seconds{kind="tests"} 12.0' in text
    assert 'ci_lab_unit_duration_seconds{kind="mutation"} 40.0' in text
    assert 'ci_lab_heads_graded_total{class="queue",conclusion="failure"} 2.0' in text


def test_record_ships_through_the_real_outbox(tmp_path: Path) -> None:
    sent: list[tuple[str, bytes]] = []
    sink = metrics_outbox.Settings("http://clickhouse.invalid:8123", "", "")

    def send(target: object, query: str, body: bytes) -> bool:
        sent.append((query, body))
        return True

    outbox = metrics_outbox.Outbox(tmp_path / "spool.db", sink, send=send)
    try:
        metrics.record(outbox, [unit()], [grade()])
        outbox.flush(1_700_000_000_002)
    finally:
        outbox.close()
    inserts = {query: body for query, body in sent if query.startswith("INSERT")}
    assert set(inserts) == {
        "INSERT INTO swarm.lab_units FORMAT JSONEachRow",
        "INSERT INTO swarm.lab_grades FORMAT JSONEachRow",
    }
    assert b'"class": "pull_request"' in inserts["INSERT INTO swarm.lab_units FORMAT JSONEachRow"]
