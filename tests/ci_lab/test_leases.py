import threading

import pytest

from scripts.ci_lab import leases, queue
from scripts.ci_lab.units import Unit


class Clock:
    def __init__(self, now: int = 1000) -> None:
        self.now = now
        self.sleeps: list[int] = []

    def __call__(self) -> int:
        return self.now

    def sleep(self, ms: int) -> None:
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


def unit(key: str) -> Unit:
    return Unit(kind="test", key=key, members=(f"tests/{key}.py::t",))


def test_lease_grants_fitting_jobs_in_order_with_distinct_tokens(jobs: queue.Queue, lab: leases.Leases) -> None:
    needs = {
        "big": queue.Requirement(slots=3),
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
    assert jobs.job("h1:test:net").worker == "w1"
    assert lab.owns("h1:test:net", grants[0].token)
    assert not lab.owns("h1:test:net", grants[1].token)


def test_lease_stops_at_free_slots(jobs: queue.Queue, lab: leases.Leases) -> None:
    jobs.enqueue([unit("a"), unit("b"), unit("c")], "h1", {})

    assert [grant.job for grant in lab.lease("w1", 2, frozenset(), 0)] == ["h1:test:a", "h1:test:b"]
    assert lab.lease("w1", 0, frozenset(), 0) == []
    assert jobs.pending() == ["h1:test:c"]


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
    assert lab.lease("w1", 1, frozenset(), 120) == []
    assert clock.sleeps == [50, 50, 20]
    assert clock.now == 1120


def test_heartbeat_keeps_the_lease(jobs: queue.Queue, lab: leases.Leases, clock: Clock) -> None:
    jobs.enqueue([unit("a")], "h1", {})
    grant = lab.lease("w1", 1, frozenset(), 0)[0]

    clock.now = 1400
    assert lab.heartbeat(grant.job, grant.token) == 1900
    clock.now = 1800
    assert lab.reap(clock.now) == []
    assert jobs.job(grant.job).state == "leased"
    assert lab.heartbeat(grant.job, "wrong") is None
    assert lab.heartbeat(grant.job, "") is None


def test_expired_lease_requeues_once_then_is_lost(jobs: queue.Queue, lab: leases.Leases, clock: Clock) -> None:
    jobs.enqueue([unit("a")], "h1", {})
    first = lab.lease("w1", 1, frozenset(), 0)[0]

    assert lab.reap(1499) == []
    assert lab.reap(1500) == [("h1:test:a", "pending")]
    assert jobs.job("h1:test:a").attempts == 1
    assert jobs.pending() == ["h1:test:a"]
    assert not lab.owns(first.job, first.token)
    assert lab.heartbeat(first.job, first.token) is None
    assert lab.complete(first.job, first.token, "passed") is False

    clock.now = 2000
    second = lab.lease("w2", 1, frozenset(), 0)[0]
    assert second.token != first.token
    assert lab.reap(2500) == [("h1:test:a", "lost")]

    lost = jobs.job("h1:test:a")
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

    done = jobs.job(grant.job)
    assert (done.state, done.outcome) == ("done", "failed")
    assert jobs.members("h1", "done") == {grant.job}
    assert lab.reap(9999) == []


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
    assert jobs.job(grant.job).state == "done"


def test_concurrent_leases_never_share_a_job(server, jobs: queue.Queue) -> None:
    jobs.enqueue([unit(f"u{index}") for index in range(60)], "h1", {})
    barrier = threading.Barrier(8)
    granted: list[list[str]] = [[] for _ in range(8)]

    def work(index: int) -> None:
        worker = leases.Leases(queue.Queue(client(server)), ttl_ms=500, clock=lambda: 1000, sleep=lambda ms: None)
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
        assert all(jobs.job(job).worker == f"w{index}" for job in jobs_of)
