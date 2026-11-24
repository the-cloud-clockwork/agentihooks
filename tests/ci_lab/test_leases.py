import json
import threading
from pathlib import Path

import pytest

from scripts import ci_wiring
from scripts.ci_lab import leases, pods, queue, static_gates
from scripts.ci_lab.units import Unit

ROOT = Path(__file__).resolve().parents[2]
TYPE_GATE = "type-gate/hold-the-strict-type-checker-s-errors-per-file-to-the-base-baseline"
TYPE_JOB = f"static:{TYPE_GATE.replace('/', '-')}"
FORMAT_GATE = "lint/format-check"


class Clock:
    def __init__(self, now: int = 1000) -> None:
        self.now = now
        self.sleeps: list[int] = []

    def __call__(self) -> int:
        return self.now

    def sleep(self, ms: int) -> None:
        assert ms > 0
        assert len(self.sleeps) < 100
        self.sleeps.append(ms)
        self.now += ms


@pytest.fixture
def server():
    import fakeredis

    return fakeredis.FakeServer()


def client(server):
    import fakeredis

    return fakeredis.FakeRedis(server=server, decode_responses=True)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def jobs(server) -> queue.Queue:
    return queue.Queue(client(server))


@pytest.fixture
def lab(jobs: queue.Queue, clock: Clock) -> leases.Leases:
    return leases.Leases(jobs, ttl_ms=500, clock=clock, sleep=clock.sleep, poll_ms=50)


def found(jobs: queue.Queue, job: str) -> queue.Job:
    record = jobs.job(job)
    assert record is not None
    return record


def unit(key: str) -> Unit:
    return Unit(kind="test", key=key, members=(f"tests/{key}.py::t",))


def gate(name: str) -> Unit:
    return Unit(kind="static", key=name.replace("/", "-"), members=(name,))


def lease_reads(jobs: queue.Queue, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    reads: list[list[str]] = []
    read = jobs.leased

    def counted() -> list[str]:
        reads.append(read())
        return reads[-1]

    monkeypatch.setattr(jobs, "leased", counted)
    return reads


def page_reads(jobs: queue.Queue, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    pages: list[dict[str, object]] = []
    read = jobs.client.zrangebyscore

    def counted(*arguments: object, **options: object) -> object:
        pages.append(options)
        return read(*arguments, **options)

    monkeypatch.setattr(jobs.client, "zrangebyscore", counted)
    return pages


def test_lease_grants_fitting_jobs_in_order_with_distinct_tokens(jobs: queue.Queue, lab: leases.Leases) -> None:
    needs = {
        "big": queue.Requirement(slots=4),
        "web": queue.Requirement(slots=1, capabilities=frozenset({"browser"})),
        "net": queue.Requirement(slots=2, capabilities=frozenset({"net"})),
    }
    jobs.enqueue([unit("big"), unit("web"), unit("net"), unit("a"), unit("b")], "h1", needs)

    grants = lab.lease("w1", 3, frozenset({"net"}), 0)

    assert [grant.job for grant in grants] == ["h1:test:net", "h1:test:a"]
    assert [grant.expires_ms for grant in grants] == [1500, 1500]
    assert [grant.unit for grant in grants] == [unit("net"), unit("a")]
    assert [grant.slots for grant in grants] == [2, 1]
    assert grants[0].head == "h1"
    assert len({grant.token for grant in grants}) == 2
    assert all(len(grant.token) == 32 for grant in grants)
    assert jobs.pending() == ["h1:test:big", "h1:test:web", "h1:test:b"]
    assert found(jobs, "h1:test:net").worker == "w1"
    assert lab.owns("h1:test:net", grants[0].token)
    assert not lab.owns("h1:test:net", grants[1].token)


@pytest.mark.feature("lab-worker-executors")
def test_a_grant_carries_the_base_and_grader_revision_of_its_job(jobs: queue.Queue, lab: leases.Leases) -> None:
    jobs.enqueue([unit("a")], "h1", {}, "b1", "g1")

    [grant] = lab.lease("w1", 1, frozenset(), 0)

    assert grant == leases.Grant("h1:test:a", grant.token, "h1", unit("a"), 1, 1500, "b1", "g1")


@pytest.mark.feature("lab-scheduler-metrics")
def test_a_grant_records_the_millisecond_its_lease_started(jobs: queue.Queue, lab: leases.Leases, clock: Clock) -> None:
    jobs.enqueue([unit("a")], "h1", {})
    clock.now = 1234

    [grant] = lab.lease("w1", 1, frozenset(), 0)

    assert (grant.expires_ms, found(jobs, "h1:test:a").leased_ms) == (1734, 1234)


def test_lease_stops_at_free_slots(jobs: queue.Queue, lab: leases.Leases) -> None:
    jobs.enqueue([unit("a"), unit("b"), unit("c")], "h1", {})

    assert [grant.job for grant in lab.lease("w1", 2, frozenset(), 0)] == ["h1:test:a", "h1:test:b"]
    assert lab.lease("w1", 0, frozenset(), 0) == []
    assert jobs.pending() == ["h1:test:c"]


def test_lease_stops_reading_once_slots_run_out(
    jobs: queue.Queue, lab: leases.Leases, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs.enqueue([unit("a"), unit("b"), unit("c"), unit("d")], "h1", {})
    reads: list[str] = []
    read = jobs.job

    def counted(job: str) -> queue.Job | None:
        reads.append(job)
        return read(job)

    monkeypatch.setattr(jobs, "job", counted)

    assert len(lab.lease("w1", 2, frozenset(), 0)) == 2
    assert reads == ["h1:test:a", "h1:test:b"]


@pytest.mark.feature("lab-lease-class-order")
def test_a_lease_grants_the_most_urgent_class_first(jobs: queue.Queue, lab: leases.Leases) -> None:
    jobs.enqueue([unit("q1"), unit("q2")], "merge", {}, rank=2)
    jobs.enqueue([unit("old")], "before", {})
    jobs.enqueue([unit("p1")], "pull", {}, rank=1)

    assert [grant.job for grant in lab.lease("w1", 2, frozenset(), 0)] == ["pull:test:p1", "merge:test:q1"]
    assert [grant.job for grant in lab.lease("w1", 2, frozenset(), 0)] == ["merge:test:q2", "before:test:old"]


@pytest.mark.feature("lab-lease-class-order")
def test_a_lease_poll_reads_one_page_of_a_long_pending_order(
    jobs: queue.Queue, lab: leases.Leases, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs.enqueue([unit(f"{number:03}") for number in range(queue.PAGE * 2 + 1)], "h1", {}, rank=1)
    pages = page_reads(jobs, monkeypatch)

    def whole(*arguments: object, **options: object) -> object:
        raise AssertionError("a lease poll read the whole pending set")

    monkeypatch.setattr(jobs.client, "zrange", whole)

    assert [grant.job for grant in lab.lease("w1", 2, frozenset(), 0)] == ["h1:test:000", "h1:test:001"]
    assert [page["num"] for page in pages] == [queue.PAGE]


@pytest.mark.feature("lab-lease-class-order")
def test_a_lease_reads_no_page_once_its_slots_are_full(
    jobs: queue.Queue, lab: leases.Leases, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs.enqueue([unit(f"{number:03}") for number in range(queue.PAGE + 1)], "h1", {}, rank=1)
    pages = page_reads(jobs, monkeypatch)

    assert len(lab.lease("w1", queue.PAGE, frozenset(), 0)) == queue.PAGE
    assert len(pages) == 1
    assert lab.lease("w1", 0, frozenset(), 0) == []
    assert len(pages) == 1


@pytest.mark.feature("lab-lease-class-order")
def test_a_lease_reads_past_a_page_of_jobs_that_do_not_fit(jobs: queue.Queue, lab: leases.Leases) -> None:
    wide = {f"{number:03}": queue.Requirement(slots=3) for number in range(queue.PAGE)}
    jobs.enqueue([unit(key) for key in wide], "h1", wide, rank=1)
    jobs.enqueue([unit("narrow")], "h2", {}, rank=2)

    assert [grant.job for grant in lab.lease("w1", 1, frozenset(), 0)] == ["h2:test:narrow"]


def test_long_poll_waits_until_work_arrives(jobs: queue.Queue, clock: Clock) -> None:
    def arrive(ms: int) -> None:
        clock.sleep(ms)
        if len(clock.sleeps) == 3:
            jobs.enqueue([unit("late")], "h1", {})

    lab = leases.Leases(jobs, ttl_ms=500, clock=clock, sleep=arrive, poll_ms=50)

    grants = lab.lease("w1", 1, frozenset(), 1000)

    assert [grant.job for grant in grants] == ["h1:test:late"]
    assert clock.sleeps == [50, 50, 50]


def test_long_poll_returns_empty_at_the_deadline(lab: leases.Leases, clock: Clock) -> None:
    assert lab.lease("w1", 1, frozenset(), 101) == []
    assert clock.sleeps == [50, 50, 1]
    assert clock.now == 1101


def test_heartbeat_keeps_the_lease(jobs: queue.Queue, lab: leases.Leases, clock: Clock) -> None:
    jobs.enqueue([unit("a")], "h1", {})
    grant = lab.lease("w1", 1, frozenset(), 0)[0]

    clock.now = 1400
    assert lab.heartbeat(grant.job, grant.token) == 1900
    clock.now = 1800
    assert lab.reap(clock.now) == []
    assert found(jobs, grant.job).state == "leased"
    assert lab.heartbeat(grant.job, "wrong") is None
    assert lab.heartbeat(grant.job, "") is None


def test_expired_lease_requeues_once_then_is_lost(jobs: queue.Queue, lab: leases.Leases, clock: Clock) -> None:
    jobs.enqueue([unit("a")], "h1", {})
    first = lab.lease("w1", 1, frozenset(), 0)[0]

    assert lab.reap(1499) == []
    assert lab.reap(1500) == [("h1:test:a", "pending")]
    assert found(jobs, "h1:test:a").attempts == 1
    assert jobs.pending() == ["h1:test:a"]
    assert not lab.owns(first.job, first.token)
    assert lab.heartbeat(first.job, first.token) is None
    assert lab.complete(first.job, first.token, "passed") is False

    clock.now = 2000
    second = lab.lease("w2", 1, frozenset(), 0)[0]
    assert second.token != first.token
    assert lab.reap(2500) == [("h1:test:a", "lost")]

    lost = found(jobs, "h1:test:a")
    assert (lost.state, lost.outcome, lost.attempts) == ("lost", "lost", 1)
    assert jobs.members("h1", "done") == {"h1:test:a"}
    assert jobs.pending() == []
    assert lab.complete(second.job, second.token, "passed") is False
    assert lab.reap(9999) == []


def test_complete_needs_the_lease_token(jobs: queue.Queue, lab: leases.Leases) -> None:
    jobs.enqueue([unit("a")], "h1", {})
    grant = lab.lease("w1", 1, frozenset(), 0)[0]

    assert lab.complete(grant.job, "wrong", "passed") is False
    assert lab.complete(grant.job, grant.token, "failed") is True
    assert lab.complete(grant.job, grant.token, "passed") is False

    done = found(jobs, grant.job)
    assert (done.state, done.outcome) == ("done", "failed")
    assert jobs.members("h1", "done") == {grant.job}
    assert lab.reap(9999) == []


@pytest.mark.feature("lab-unit-outcomes")
def test_complete_refuses_an_unknown_outcome_and_records_nothing(jobs: queue.Queue, lab: leases.Leases) -> None:
    jobs.enqueue([unit("a")], "h1", {})
    grant = lab.lease("w1", 1, frozenset(), 0)[0]

    for outcome in ("taken", "lost", "dropped", ""):
        assert lab.complete(grant.job, grant.token, outcome) is False

    held = found(jobs, grant.job)
    assert (held.state, held.outcome) == ("leased", "")
    assert lab.owns(grant.job, grant.token)


@pytest.mark.feature("lab-unit-outcomes")
@pytest.mark.parametrize("outcome", ["passed", "failed", "gate_failed", "survived", "crash", "cannot_run"])
def test_complete_records_every_outcome_a_worker_reports(jobs: queue.Queue, lab: leases.Leases, outcome: str) -> None:
    jobs.enqueue([unit("a")], "h1", {})
    grant = lab.lease("w1", 1, frozenset(), 0)[0]

    assert lab.complete(grant.job, grant.token, outcome) is True

    done = found(jobs, grant.job)
    assert (done.state, done.outcome) == ("done", outcome)


def test_unleased_job_has_no_owner(jobs: queue.Queue, lab: leases.Leases) -> None:
    jobs.enqueue([unit("a")], "h1", {})

    assert not lab.owns("h1:test:a", "")
    assert not lab.owns("h1:test:missing", "")
    assert lab.complete("h1:test:a", "", "passed") is False


def test_superseded_leased_job_still_finishes(jobs: queue.Queue, lab: leases.Leases) -> None:
    jobs.enqueue([unit("a"), unit("b")], "old", {})
    grant = lab.lease("w1", 1, frozenset(), 0)[0]

    assert jobs.supersede("old") == ["old:test:b"]
    assert lab.complete(grant.job, grant.token, "passed") is True
    assert found(jobs, grant.job).state == "done"


def refuse(ms: int) -> None:
    raise AssertionError(f"a lease without a wait slept {ms} ms")


def test_concurrent_leases_never_share_a_job(server, jobs: queue.Queue) -> None:
    jobs.enqueue([unit(f"u{index}") for index in range(60)], "h1", {})
    barrier = threading.Barrier(8)
    granted: list[list[str]] = [[] for _ in range(8)]

    def work(index: int) -> None:
        worker = leases.Leases(queue.Queue(client(server)), ttl_ms=500, clock=lambda: 1000, sleep=refuse, poll_ms=50)
        barrier.wait()
        while grants := worker.lease(f"w{index}", 2, frozenset(), 0):
            granted[index].extend(grant.job for grant in grants)

    threads = [threading.Thread(target=work, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    every = [job for jobs_of in granted for job in jobs_of]
    assert sorted(every) == sorted(f"h1:test:u{index}" for index in range(60))
    assert len(set(every)) == 60
    for index, jobs_of in enumerate(granted):
        assert all(found(jobs, job).worker == f"w{index}" for job in jobs_of)


def test_lease_skips_a_job_whose_record_vanished(jobs: queue.Queue, lab: leases.Leases) -> None:
    jobs.enqueue([unit("a"), unit("b")], "h1", {})
    jobs.client.delete("agentihooks-ci:queue:job:h1:test:a")

    assert [grant.job for grant in lab.lease("w1", 2, frozenset(), 0)] == ["h1:test:b"]


def test_reap_skips_vanished_and_live_leases(
    jobs: queue.Queue, lab: leases.Leases, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs.enqueue([unit("a"), unit("b")], "h1", {})
    lab.lease("w1", 2, frozenset(), 0)
    jobs.client.delete("agentihooks-ci:queue:job:h1:test:a")

    assert lab.reap(1500) == [("h1:test:b", "pending")]
    assert [grant.job for grant in lab.lease("w2", 1, frozenset(), 0)] == ["h1:test:b"]
    monkeypatch.setattr(jobs, "expired", lambda now_ms: ["h1:test:b"])
    assert lab.reap(1499) == []
    assert found(jobs, "h1:test:b").state == "leased"


def test_complete_of_an_unknown_job_is_refused(lab: leases.Leases) -> None:
    assert lab.complete("h1:test:none", "tok", "passed") is False


@pytest.mark.feature("lab-scheduler-service")
def test_a_lease_poll_offers_the_workers_free_slots_while_it_waits_and_what_its_grants_leave(
    jobs: queue.Queue, clock: Clock
) -> None:
    seen: list[dict[str, tuple[int, int]]] = []

    def wait(ms: int) -> None:
        seen.append(jobs.offers(0))
        clock.sleep(ms)

    lab = leases.Leases(jobs, ttl_ms=500, clock=clock, sleep=wait, poll_ms=50)
    assert lab.lease("w1", 3, frozenset(), 60) == []
    assert seen == [{"w1": (3, 1000)}, {"w1": (3, 1000)}]
    assert jobs.offers(0) == {"w1": (3, 1060)}
    jobs.enqueue([unit("a"), unit("b")], "h1", {"b": queue.Requirement(slots=2)})
    assert len(lab.lease("w1", 4, frozenset(), 0)) == 2
    assert jobs.offers(0) == {"w1": (1, 1060)}


class FakeKube:
    def __init__(self) -> None:
        self.patches: list[tuple[str, bool]] = []

    def __call__(self, pod: str, busy: bool) -> bool:
        self.patches.append((pod, busy))
        return True


def costed(jobs: queue.Queue, clock: Clock) -> tuple[leases.Leases, FakeKube]:
    kube = FakeKube()
    return leases.Leases(jobs, ttl_ms=500, clock=clock, sleep=clock.sleep, poll_ms=50, pods=kube), kube


@pytest.mark.feature("lab-worker-deletion-cost")
def test_a_granting_poll_marks_the_worker_pod_busy_and_a_poll_granting_nothing_patches_nothing(
    jobs: queue.Queue, clock: Clock
) -> None:
    lab, kube = costed(jobs, clock)
    assert lab.lease("w1", 2, frozenset(), 0) == []
    assert kube.patches == []
    jobs.enqueue([unit("a"), unit("b")], "h1", {})
    assert len(lab.lease("w1", 2, frozenset(), 0)) == 2
    assert kube.patches == [("w1", True)]


@pytest.mark.feature("lab-worker-deletion-cost")
def test_the_worker_pod_cost_clears_only_when_its_last_lease_completes(jobs: queue.Queue, clock: Clock) -> None:
    lab, kube = costed(jobs, clock)
    jobs.enqueue([unit("a"), unit("b"), unit("c")], "h1", {})
    first, second = lab.lease("w1", 2, frozenset(), 0)
    other = lab.lease("w2", 1, frozenset(), 0)[0]
    kube.patches.clear()
    assert lab.complete(first.job, "wrong", "passed") is False
    assert lab.complete(first.job, first.token, "taken") is False
    assert lab.complete(first.job, first.token, "passed") is True
    assert kube.patches == []
    assert lab.complete(second.job, second.token, "failed") is True
    assert kube.patches == [("w1", False)]
    assert lab.complete(other.job, other.token, "passed") is True
    assert kube.patches == [("w1", False), ("w2", False)]


@pytest.mark.feature("lab-worker-deletion-cost")
def test_a_reaped_last_lease_clears_the_worker_pod_cost_whether_requeued_or_lost(
    jobs: queue.Queue, clock: Clock
) -> None:
    lab, kube = costed(jobs, clock)
    jobs.enqueue([unit("a")], "h1", {})
    lab.lease("w1", 1, frozenset(), 0)
    assert lab.reap(1500) == [("h1:test:a", "pending")]
    lab.lease("w2", 1, frozenset(), 0)
    assert lab.reap(2000) == [("h1:test:a", "lost")]
    assert kube.patches == [("w1", True), ("w1", False), ("w2", True), ("w2", False)]


class RacedKube(FakeKube):
    def __init__(self, jobs: queue.Queue, job: str) -> None:
        super().__init__()
        self.jobs, self.job = jobs, job

    def __call__(self, pod: str, busy: bool) -> bool:
        if not busy and self.job:
            raced = found(self.jobs, self.job)
            assert self.jobs.claim(raced, pod, "raced", 9999, 0)
            self.job = ""
        return super().__call__(pod, busy)


@pytest.mark.feature("lab-worker-deletion-cost")
def test_a_grant_landing_while_the_last_lease_clears_its_cost_marks_the_worker_pod_busy_again(
    jobs: queue.Queue, clock: Clock
) -> None:
    jobs.enqueue([unit("a"), unit("b")], "h1", {})
    kube = RacedKube(jobs, "h1:test:b")
    lab = leases.Leases(jobs, ttl_ms=500, clock=clock, sleep=clock.sleep, poll_ms=50, pods=kube)
    grant = lab.lease("w1", 1, frozenset(), 0)[0]
    assert lab.complete(grant.job, grant.token, "passed") is True
    assert kube.patches == [("w1", True), ("w1", False), ("w1", True)]
    assert lab.owns("h1:test:b", "raced")
    assert lab.complete("h1:test:b", "raced", "passed") is True
    assert kube.patches[3:] == [("w1", False)]


def staled(jobs: queue.Queue, clock: Clock, image: str, images: dict[str, pods.Images | None]) -> leases.Leases:
    def reader(worker: str) -> pods.Images | None:
        return images.get(worker)

    return leases.Leases(jobs, ttl_ms=500, clock=clock, sleep=clock.sleep, poll_ms=50, image=image, images=reader)


@pytest.mark.feature("lab-worker-seat-keeping-restart")
def test_stale_is_true_for_a_floating_pod_running_another_digest_than_the_wanted_one(
    jobs: queue.Queue, clock: Clock
) -> None:
    lab = staled(
        jobs,
        clock,
        "repo/lab-worker@sha256:wanted",
        {"w1": pods.Images("repo/lab-worker:dev", "repo/lab-worker@sha256:running")},
    )
    assert lab.stale("w1") is True


@pytest.mark.feature("lab-worker-seat-keeping-restart")
def test_stale_is_false_with_no_wanted_image(jobs: queue.Queue, clock: Clock) -> None:
    lab = staled(jobs, clock, "", {"w1": pods.Images("repo/lab-worker:dev", "repo/lab-worker@sha256:running")})
    assert lab.stale("w1") is False


@pytest.mark.feature("lab-worker-seat-keeping-restart")
def test_stale_is_false_with_a_wanted_image_that_names_no_digest(jobs: queue.Queue, clock: Clock) -> None:
    lab = staled(
        jobs,
        clock,
        "repo/lab-worker:release",
        {"w1": pods.Images("repo/lab-worker:dev", "repo/lab-worker@sha256:running")},
    )
    assert lab.stale("w1") is False


@pytest.mark.feature("lab-worker-seat-keeping-restart")
def test_stale_is_false_with_an_unknown_running_image(jobs: queue.Queue, clock: Clock) -> None:
    lab = staled(jobs, clock, "repo/lab-worker@sha256:wanted", {})
    assert lab.stale("w1") is False


@pytest.mark.feature("lab-worker-seat-keeping-restart")
def test_stale_is_false_when_the_running_digest_matches_under_another_repository_name(
    jobs: queue.Queue, clock: Clock
) -> None:
    lab = staled(
        jobs,
        clock,
        "repo/lab-worker@sha256:wanted",
        {"w1": pods.Images("mirror/lab-worker:dev", "other/lab-worker@sha256:wanted")},
    )
    assert lab.stale("w1") is False


@pytest.mark.feature("lab-worker-seat-keeping-restart")
def test_stale_is_false_for_a_pod_pinned_by_digest_even_when_its_running_digest_differs(
    jobs: queue.Queue, clock: Clock
) -> None:
    lab = staled(
        jobs,
        clock,
        "repo/lab-worker@sha256:wanted",
        {"w1": pods.Images("repo/lab-worker@sha256:pinned", "repo/lab-worker@sha256:running")},
    )
    assert lab.stale("w1") is False


@pytest.mark.feature("lab-type-gate-one-per-worker")
def test_static_gates_of_other_jobs_fill_a_worker_at_one_slot_each(jobs: queue.Queue, lab: leases.Leases) -> None:
    jobs.enqueue([gate(FORMAT_GATE)], "h1", {})
    jobs.enqueue([gate(FORMAT_GATE)], "h2", {})

    grants = lab.lease("w1", 2, frozenset(), 0)

    assert [grant.job for grant in grants] == ["h1:static:lint-format-check", "h2:static:lint-format-check"]
    assert [grant.slots for grant in grants] == [1, 1]


@pytest.mark.feature("lab-type-gate-one-per-worker")
def test_units_of_other_kinds_fill_a_worker_whatever_their_members_name(jobs: queue.Queue, lab: leases.Leases) -> None:
    named = [Unit(kind="test", key=key, members=(TYPE_GATE,)) for key in ("a", "b")]
    jobs.enqueue([*named, Unit(kind="stats", key="bare", members=())], "h1", {})

    grants = lab.lease("w1", 3, frozenset(), 0)

    assert [grant.job for grant in grants] == ["h1:test:a", "h1:test:b", "h1:stats:bare"]


@pytest.mark.feature("lab-type-gate-one-per-worker")
def test_a_lease_of_units_that_share_a_worker_reads_no_lease_record(
    jobs: queue.Queue, lab: leases.Leases, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs.enqueue([unit("a"), gate(FORMAT_GATE)], "h1", {})
    reads: list[str] = []
    monkeypatch.setattr(jobs, "leased", lambda: reads.append("leased") or [])

    assert len(lab.lease("w1", 2, frozenset(), 0)) == 2
    assert reads == []


@pytest.mark.feature("lab-type-gate-one-per-worker")
def test_one_take_grants_a_worker_one_type_gate_unit_and_reads_its_leases_once(
    jobs: queue.Queue, lab: leases.Leases, monkeypatch: pytest.MonkeyPatch
) -> None:
    for head in ("h1", "h2", "h3"):
        jobs.enqueue([gate(TYPE_GATE)], head, {})
    jobs.enqueue([gate(FORMAT_GATE)], "h3", {})
    reads = lease_reads(jobs, monkeypatch)

    grants = lab.lease("w1", 5, frozenset(), 0)

    assert [grant.job for grant in grants] == [f"h1:{TYPE_JOB}", "h3:static:lint-format-check"]
    assert [grant.slots for grant in grants] == [1, 1]
    assert jobs.pending() == [f"h2:{TYPE_JOB}", f"h3:{TYPE_JOB}"]
    assert reads == [[]]


@pytest.mark.feature("lab-type-gate-one-per-worker")
def test_a_worker_that_holds_a_type_gate_unit_takes_the_next_one_only_after_it_completes(
    jobs: queue.Queue, lab: leases.Leases
) -> None:
    jobs.enqueue([gate(TYPE_GATE)], "h1", {})
    jobs.enqueue([gate(TYPE_GATE), unit("a")], "h2", {})
    held = lab.lease("w1", 1, frozenset(), 0)
    assert [grant.job for grant in held] == [f"h1:{TYPE_JOB}"]

    assert [grant.job for grant in lab.lease("w1", 4, frozenset(), 0)] == ["h2:test:a"]
    assert lab.lease("w1", 4, frozenset(), 0) == []
    assert lab.complete(held[0].job, held[0].token, "passed") is True

    assert [grant.job for grant in lab.lease("w1", 4, frozenset(), 0)] == [f"h2:{TYPE_JOB}"]


@pytest.mark.feature("lab-type-gate-one-per-worker")
def test_another_worker_takes_the_type_gate_unit_a_worker_that_holds_one_leaves(
    jobs: queue.Queue, lab: leases.Leases
) -> None:
    jobs.enqueue([gate(TYPE_GATE)], "h1", {})
    jobs.enqueue([gate(TYPE_GATE)], "h2", {})
    assert [grant.job for grant in lab.lease("w1", 1, frozenset(), 0)] == [f"h1:{TYPE_JOB}"]

    assert lab.lease("w1", 1, frozenset(), 0) == []
    assert [grant.job for grant in lab.lease("w2", 1, frozenset(), 0)] == [f"h2:{TYPE_JOB}"]


@pytest.mark.feature("lab-type-gate-one-per-worker")
def test_a_reaped_type_gate_lease_frees_its_worker_for_one_more(jobs: queue.Queue, lab: leases.Leases) -> None:
    jobs.enqueue([gate(TYPE_GATE)], "h1", {})
    jobs.enqueue([gate(TYPE_GATE)], "h2", {})
    lab.lease("w1", 1, frozenset(), 0)
    assert lab.reap(1500) == [(f"h1:{TYPE_JOB}", "pending")]

    assert [grant.job for grant in lab.lease("w1", 5, frozenset(), 0)] == [f"h2:{TYPE_JOB}"]
    assert jobs.pending() == [f"h1:{TYPE_JOB}"]


@pytest.mark.feature("lab-type-gate-one-per-worker")
def test_a_lease_whose_record_vanished_holds_no_type_gate_unit(jobs: queue.Queue, lab: leases.Leases) -> None:
    jobs.enqueue([gate(TYPE_GATE)], "h1", {})
    jobs.enqueue([gate(TYPE_GATE)], "h2", {})
    lab.lease("w1", 1, frozenset(), 0)
    jobs.client.delete(f"agentihooks-ci:queue:job:h1:{TYPE_JOB}")

    assert [grant.job for grant in lab.lease("w1", 1, frozenset(), 0)] == [f"h2:{TYPE_JOB}"]


@pytest.mark.feature("lab-type-gate-one-per-worker")
def test_only_a_static_gate_of_a_named_job_is_a_unit_a_worker_runs_one_at_a_time() -> None:
    assert leases.solo(gate(TYPE_GATE))
    assert leases.solo(gate("type-gate/checker/hold-the-errors-of-a-called-workflow"))
    assert not leases.solo(gate(FORMAT_GATE))
    assert not leases.solo(Unit(kind="test", key="a", members=(TYPE_GATE,)))
    assert not leases.solo(Unit(kind="static", key="bare", members=()))
    assert not leases.solo(Unit(kind="static", key="later", members=(FORMAT_GATE, TYPE_GATE)))


@pytest.mark.feature("lab-type-gate-one-per-worker")
def test_every_discovered_gate_that_runs_the_type_checker_is_held_to_one_a_worker() -> None:
    registry = json.loads((ROOT / static_gates.REGISTRY).read_text())
    workflow = (ROOT / static_gates.WORKFLOW).read_text()
    gates = static_gates.discover(workflow, registry, ci_wiring.load(ROOT))
    checkers = [found.name for found in gates if "scripts.type_gate" in found.modules]

    assert checkers == [TYPE_GATE]
    assert all(leases.solo(gate(name)) for name in checkers)
    assert {job for _, job in leases.SOLO} <= set(static_gates.STATIC_JOBS)
