"""Settled lab queue job records expire after the scheduler's retention, retry and requeue clear it, and the scheduler
drains the old global done set; kept out of tests/ci_lab/test_scheduler.py, which is at the file-lines cap."""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from scripts.ci_lab import queue, relay, scheduler
from scripts.ci_lab.delivery import CLASSES
from scripts.ci_lab.leases import Leases
from scripts.ci_lab.objects import LocalObjects
from scripts.ci_lab.outcomes import CRASH, DROPPED, PASSED
from scripts.ci_lab.retry import Policy
from scripts.ci_lab.units import Unit
from tests.ci_lab.test_scheduler import (
    APP_SETTINGS,
    HEAD_A,
    Expander,
    Inbox,
    Poster,
    RelayBuilds,
    Run,
    fake_redis,
    key,
    plan,
    ref,
    unit,
)
from tests.ci_lab.test_scheduler import lab as lab

DONE = queue.PREFIX + "done"
RETENTION_MS = 60_000


def record_key(job: str) -> str:
    return f"{queue.PREFIX}job:{job}"


def head_done(head: str) -> str:
    return f"{queue.PREFIX}head:{head}:done"


def expires_within(client: Any, name: str, retention_ms: int) -> bool:
    return 0 < client.pttl(name) <= retention_ms


@pytest.fixture
def client() -> Any:
    return fake_redis()


@pytest.fixture
def jobs(client: Any) -> queue.Queue:
    return queue.Queue(client)


def leased(jobs: queue.Queue, name: str, expires_ms: int = 100) -> queue.Job:
    jobs.enqueue([Unit("test", name, (f"tests/{name}.py::a",))], "h1", {})
    record = jobs.job(f"h1:test:{name}")
    assert record is not None
    assert jobs.claim(record, "w1", "tok", expires_ms, 0)
    claimed = jobs.job(record.id)
    assert claimed is not None
    return claimed


@pytest.mark.feature("lab-settled-job-retention")
def test_a_grade_takes_a_settled_job_s_outcome_and_drops_a_slot_whose_job_record_is_gone(
    lab: Callable[..., Run],
) -> None:
    kept, gone = unit(1), unit(2)
    run = lab({HEAD_A: plan({"lint": (kept, gone)})})
    run.push(HEAD_A)
    run.tick(1000)
    for _ in (kept, gone):
        run.finish(run.lease()[0], PASSED, {"seconds": 1.0})
    run.lab.client.delete(record_key(scheduler.job_id(HEAD_A, gone)))

    run.tick(1100)

    units = run.grade(HEAD_A).units
    assert (units[ref(kept)].outcome, units[ref(gone)].outcome) == (PASSED, DROPPED)


@pytest.mark.feature("lab-settled-job-retention")
def test_compaction_keeps_working_when_the_old_done_set_names_a_job_whose_record_is_gone(
    client: Any, tmp_path: Path
) -> None:
    from scripts.ci_lab import compact

    stored = key("test", 1)
    client.hset(record_key("h1:test:stored"), mapping={"kind": "test", "key": stored, "manifest": "tests/a.py::a"})
    client.sadd(DONE, "h1:test:stored", "h1:test:gone")

    counts = compact.compact(client, LocalObjects(tmp_path / "objects"))

    assert (counts["job manifests moved"], counts["jobs stripped"]) == (1, 1)
    assert client.hget(record_key("h1:test:stored"), "manifest") is None


@pytest.mark.feature("lab-settled-job-retention")
def test_the_retention_default_outlives_an_hour_and_is_the_scheduler_setting_default() -> None:
    assert queue.RETENTION_MS >= 60 * 60 * 1000
    assert scheduler.Settings(frozenset()).retention_ms == queue.RETENTION_MS


@pytest.mark.feature("lab-settled-job-retention")
def test_finish_sets_the_retention_on_the_job_record_and_its_head_done_set(jobs: queue.Queue, client: Any) -> None:
    assert jobs.finish(leased(jobs, "a"), "tok", PASSED, RETENTION_MS)

    assert expires_within(client, record_key("h1:test:a"), RETENTION_MS)
    assert expires_within(client, head_done("h1"), RETENTION_MS)


@pytest.mark.feature("lab-settled-job-retention")
def test_a_lost_reap_sets_the_retention_and_a_requeue_reap_leaves_the_record_without_one(
    jobs: queue.Queue, client: Any
) -> None:
    record = leased(jobs, "a")
    client.pexpire(record_key("h1:test:a"), RETENTION_MS)

    assert jobs.reap(record, 100, 1, RETENTION_MS) == "pending"
    assert client.pttl(record_key("h1:test:a")) == -1

    again = jobs.job("h1:test:a")
    assert again is not None
    assert jobs.claim(again, "w2", "t2", 300, 0)
    assert jobs.reap(again, 300, 1, RETENTION_MS) == "lost"
    assert expires_within(client, record_key("h1:test:a"), RETENTION_MS)
    assert expires_within(client, head_done("h1"), RETENTION_MS)


@pytest.mark.feature("lab-settled-job-retention")
def test_retry_clears_the_retention_of_a_settled_job(jobs: queue.Queue, client: Any) -> None:
    record = leased(jobs, "a")
    assert jobs.finish(record, "tok", CRASH, RETENTION_MS)
    assert expires_within(client, record_key("h1:test:a"), RETENTION_MS)

    assert jobs.retry(record) is True

    assert client.pttl(record_key("h1:test:a")) == -1


@pytest.mark.feature("lab-settled-job-retention")
def test_finish_reap_and_retry_leave_the_old_global_done_set_unwritten(jobs: queue.Queue, client: Any) -> None:
    finished, reaped = leased(jobs, "a"), leased(jobs, "b")
    client.sadd(DONE, "h0:test:old")

    assert jobs.finish(finished, "tok", CRASH)
    assert jobs.reap(reaped, 100, 0) == "lost"
    assert jobs.retry(finished) is True

    assert client.smembers(DONE) == {"h0:test:old"}


@pytest.mark.feature("lab-settled-job-retention")
def test_drain_removes_each_named_job_and_gives_only_done_or_lost_records_the_retention(
    jobs: queue.Queue, client: Any
) -> None:
    done, lost = leased(jobs, "a"), leased(jobs, "b")
    leased(jobs, "c")
    jobs.enqueue([Unit("test", "d", ("tests/d.py::a",))], "h1", {})
    assert jobs.finish(done, "tok", PASSED)
    assert jobs.reap(lost, 100, 0) == "lost"
    for name in (record_key("h1:test:a"), record_key("h1:test:b"), head_done("h1")):
        client.persist(name)
    named = ["h1:test:a", "h1:test:b", "h1:test:c", "h1:test:d", "h1:test:gone"]
    client.sadd(DONE, *named)

    assert jobs.drain(named, RETENTION_MS) == 2

    assert client.smembers(DONE) == set()
    assert [expires_within(client, record_key(job), RETENTION_MS) for job in named[:4]] == [True, True, False, False]
    assert expires_within(client, head_done("h1"), RETENTION_MS)


@pytest.mark.feature("lab-settled-job-retention")
def test_each_pass_drains_at_most_one_batch_of_the_old_done_set_then_stops_reading_it(
    lab: Callable[..., Run],
) -> None:
    run = lab({})
    run.engine.settings = replace(run.engine.settings, retention_ms=RETENTION_MS)
    client = run.lab.client
    batch = scheduler.DRAINED_PER_PASS
    named = [f"h{number % 3}:test:{number}" for number in range(batch + 1)]
    for job in named:
        client.hset(record_key(job), mapping={"head": job.partition(":")[0], "state": "done"})
    client.sadd(DONE, *named)

    first = run.tick(1000)

    assert client.scard(DONE) == 1
    assert f"drained {batch} jobs of the old done set, {batch} settled, 1 left" in first
    run.tick(1100)
    assert client.scard(DONE) == 0
    assert all(expires_within(client, record_key(job), RETENTION_MS) for job in named)
    assert not any(action.startswith("drained") for action in run.tick(1200))
    client.sadd(DONE, "h9:test:late")
    run.tick(1300)
    assert client.smembers(DONE) == {"h9:test:late"}


@pytest.mark.feature("lab-settled-job-retention")
def test_a_settled_job_keeps_its_record_within_the_retention_until_its_grade_collects_it(
    lab: Callable[..., Run],
) -> None:
    item = unit(1)
    run = lab({HEAD_A: plan({"lint": (item,)})})
    run.push(HEAD_A)
    run.tick(1000)
    run.finish(run.lease()[0], PASSED, {"seconds": 1.0})

    assert expires_within(run.lab.client, record_key(scheduler.job_id(HEAD_A, item)), queue.RETENTION_MS)
    run.tick(1100)
    assert run.grade(HEAD_A).units[ref(item)].outcome == PASSED


@pytest.mark.feature("lab-settled-job-retention")
def test_the_reap_pass_marks_a_lost_job_with_the_scheduler_retention(lab: Callable[..., Run]) -> None:
    item = unit(1)
    run = lab({HEAD_A: plan({"lint": (item,)})}, retries=Policy(lost=0))
    run.engine.settings = replace(run.engine.settings, retention_ms=RETENTION_MS)
    run.push(HEAD_A)
    run.tick(1000)
    [grant] = run.lease(ttl_ms=100)

    assert f"reaped {grant.job} lost" in run.tick(1100)

    assert expires_within(run.lab.client, record_key(grant.job), RETENTION_MS)
    assert expires_within(run.lab.client, head_done(HEAD_A), RETENTION_MS)


@pytest.mark.feature("lab-settled-job-retention")
def test_lease_completion_and_reaping_settle_with_the_lease_layer_retention(jobs: queue.Queue, client: Any) -> None:
    lab = Leases(jobs, 100, lambda: 0, lambda _: None, 1, retention_ms=RETENTION_MS)
    leased(jobs, "a")
    leased(jobs, "b")

    assert lab.complete("h1:test:a", "tok", PASSED)
    assert lab.reap(100) == [("h1:test:b", "pending")]
    again = jobs.job("h1:test:b")
    assert again is not None
    assert jobs.claim(again, "w2", "t2", 300, 0)
    assert lab.reap(300) == [("h1:test:b", "lost")]

    assert expires_within(client, record_key("h1:test:a"), RETENTION_MS)
    assert expires_within(client, record_key("h1:test:b"), RETENTION_MS)


@pytest.mark.feature("lab-settled-job-retention")
def test_main_gives_leases_the_scheduler_retention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import boto3  # pyright: ignore[reportMissingTypeStubs]
    import redis

    from scripts.events import config, factory

    client = fake_redis()
    listened: dict[str, Leases] = {}
    bus = SimpleNamespace(lab_heads=SimpleNamespace(consumer=lambda durable, subject: "consumer"))

    def build(environ: Any, redis_client: Any, objects: Any, consumer: Any, spending: Any) -> scheduler.Scheduler:
        ports = scheduler.Ports(Inbox(), Expander({}), lambda repo: Poster(), lambda: frozenset())
        settings = scheduler.Settings(frozenset(), retention_ms=RETENTION_MS)
        return scheduler.Scheduler(redis_client, objects, ports, settings, spending)

    def listen(found: Any, leases: Leases, *rest: Any) -> list[Any]:
        listened["leases"] = leases
        return []

    runtime = tmp_path / "runtime.yaml"
    runtime.write_text(
        yaml.safe_dump(
            {
                "classes": list(CLASSES),
                "budget": {"coreMinutesPerDay": 600, "shed": {"backfill": 0.5}},
                "backfill": {"auditShare": 0.1, "moduleSweeps": False},
            }
        )
    )
    environ = {
        scheduler.contract.REDIS_URL: "redis://lab",
        scheduler.contract.RUNTIME_CONFIG: str(runtime),
        scheduler.MINIO_ENDPOINT: "endpoint",
        scheduler.MINIO_ACCESS_KEY: "access",
        scheduler.MINIO_SECRET_KEY: "secret",
        scheduler.MINIO_BUCKET: "bucket",
        scheduler.contract.GIT_MIRROR: str(tmp_path / "node"),
    } | APP_SETTINGS
    monkeypatch.setattr(redis.Redis, "from_url", lambda url, **options: client)
    monkeypatch.setattr(boto3, "client", lambda name, **options: "s3 client")
    monkeypatch.setattr(
        config.BusConfig, "from_environ", classmethod(lambda cls, found: config.BusConfig("nats", "nats://lab"))
    )
    monkeypatch.setattr(factory, "open_bus", lambda found: bus)
    monkeypatch.setattr(scheduler, "build", build)
    monkeypatch.setattr(scheduler, "listen", listen)
    monkeypatch.setattr(scheduler, "serve", lambda *args: 7)
    monkeypatch.setattr(relay, "build", RelayBuilds())

    assert scheduler.main([], environ) == 7

    assert listened["leases"].retention_ms == RETENTION_MS
