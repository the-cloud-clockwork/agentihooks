import pytest

from scripts.ci_lab import queue
from scripts.ci_lab.units import Unit


@pytest.fixture
def client():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


@pytest.fixture
def jobs(client) -> queue.Queue:
    return queue.Queue(client)


def found(jobs: queue.Queue, job: str) -> queue.Job:
    record = jobs.job(job)
    assert record is not None
    return record


def unit(key: str, kind: str = "test") -> Unit:
    return Unit(kind=kind, key=key, members=(f"tests/{key}.py::a", f"tests/{key}.py::b"))


def test_enqueue_records_pending_jobs_in_order_per_head(jobs: queue.Queue) -> None:
    needs = {"b": queue.Requirement(slots=3, capabilities=frozenset({"net", "browser"}))}

    ids = jobs.enqueue([unit("a"), unit("b"), unit("c", kind="mutant")], "h1", needs)

    assert ids == ["h1:test:a", "h1:test:b", "h1:mutant:c"]
    assert jobs.pending() == ids
    assert jobs.members("h1", "pending") == set(ids)
    assert jobs.members("h1", "leased") == set()
    assert jobs.members("h1", "done") == set()
    job = found(jobs, "h1:test:b")
    assert job == queue.Job(
        id="h1:test:b",
        head="h1",
        unit=unit("b"),
        requirement=queue.Requirement(slots=3, capabilities=frozenset({"net", "browser"})),
        state="pending",
        attempts=0,
        worker="",
        outcome="",
    )
    assert found(jobs, "h1:test:a").requirement == queue.Requirement(slots=1, capabilities=frozenset())


def take(jobs: queue.Queue, job: str, worker: str = "w1", token: str = "tok", expires_ms: int = 100) -> bool:
    return jobs.claim(found(jobs, job), worker, token, expires_ms)


def test_enqueue_skips_jobs_already_known(jobs: queue.Queue) -> None:
    jobs.enqueue([unit("a")], "h1", {})
    assert take(jobs, "h1:test:a")

    assert jobs.enqueue([unit("a"), unit("b")], "h1", {}) == ["h1:test:b"]
    assert jobs.pending() == ["h1:test:b"]
    assert found(jobs, "h1:test:a").state == "leased"


def test_same_unit_on_two_heads_is_two_jobs(jobs: queue.Queue) -> None:
    assert jobs.enqueue([unit("a")], "h1", {}) == ["h1:test:a"]
    assert jobs.enqueue([unit("a")], "h2", {}) == ["h2:test:a"]
    assert jobs.pending() == ["h1:test:a", "h2:test:a"]
    assert jobs.members("h2", "pending") == {"h2:test:a"}


def test_supersede_drops_pending_and_keeps_leased(jobs: queue.Queue) -> None:
    jobs.enqueue([unit("a"), unit("b"), unit("c")], "old", {})
    jobs.enqueue([unit("a")], "new", {})
    assert take(jobs, "old:test:b")

    assert jobs.supersede("old") == ["old:test:a", "old:test:c"]

    assert jobs.pending() == ["new:test:a"]
    assert jobs.job("old:test:a") is None
    assert jobs.job("old:test:c") is None
    assert found(jobs, "old:test:b").state == "leased"
    assert jobs.members("old", "pending") == set()
    assert jobs.members("old", "leased") == {"old:test:b"}
    assert jobs.members("new", "pending") == {"new:test:a"}
    assert jobs.supersede("old") == []


def test_claim_is_won_once(jobs: queue.Queue) -> None:
    jobs.enqueue([unit("a")], "h1", {})
    record = found(jobs, "h1:test:a")

    assert jobs.claim(record, "w1", "t1", 100) is True
    assert jobs.claim(record, "w2", "t2", 100) is False
    assert jobs.pending() == []
    assert (found(jobs, "h1:test:a").worker, jobs.token("h1:test:a")) == ("w1", "t1")


def test_claim_records_the_lease(jobs: queue.Queue) -> None:
    jobs.enqueue([unit("a"), unit("b")], "h1", {})
    assert take(jobs, "h1:test:a", "w1", "tok", 100)

    leased = found(jobs, "h1:test:a")
    assert (leased.state, leased.worker) == ("leased", "w1")
    assert jobs.token("h1:test:a") == "tok"
    assert jobs.members("h1", "leased") == {"h1:test:a"}
    assert jobs.members("h1", "pending") == {"h1:test:b"}
    assert jobs.expired(99) == []
    assert jobs.expired(100) == ["h1:test:a"]


def test_extend_needs_the_token_and_a_live_lease(jobs: queue.Queue) -> None:
    jobs.enqueue([unit("a")], "h1", {})
    assert jobs.extend("h1:test:a", "", 200) is False
    assert take(jobs, "h1:test:a")

    assert jobs.extend("h1:test:a", "wrong", 200) is False
    assert jobs.extend("h1:test:a", "", 200) is False
    assert jobs.expired(150) == ["h1:test:a"]
    assert jobs.extend("h1:test:a", "tok", 200) is True
    assert jobs.expired(150) == []
    assert jobs.expired(200) == ["h1:test:a"]


def test_finish_needs_the_token_once(jobs: queue.Queue) -> None:
    jobs.enqueue([unit("a")], "h1", {})
    assert take(jobs, "h1:test:a")
    record = found(jobs, "h1:test:a")

    assert jobs.finish(record, "wrong", "passed") is False
    assert jobs.finish(record, "", "passed") is False
    assert jobs.finish(record, "tok", "failed") is True
    assert jobs.finish(record, "tok", "passed") is False

    done = found(jobs, "h1:test:a")
    assert (done.state, done.outcome, done.worker) == ("done", "failed", "w1")
    assert jobs.token("h1:test:a") == ""
    assert jobs.members("h1", "done") == {"h1:test:a"}
    assert jobs.members("h1", "leased") == set()
    assert jobs.expired(10**9) == []


def test_reap_requeues_while_attempts_remain_then_marks_lost(jobs: queue.Queue) -> None:
    jobs.enqueue([unit("a"), unit("b")], "h1", {})
    assert take(jobs, "h1:test:a")
    record = found(jobs, "h1:test:a")

    assert jobs.reap(record, 99, 1) == ""
    assert jobs.reap(record, 100, 1) == "pending"
    assert jobs.reap(record, 100, 1) == ""

    again = found(jobs, "h1:test:a")
    assert (again.state, again.attempts, again.worker) == ("pending", 1, "")
    assert jobs.token("h1:test:a") == ""
    assert jobs.pending() == ["h1:test:b", "h1:test:a"]
    assert jobs.members("h1", "pending") == {"h1:test:a", "h1:test:b"}
    assert jobs.members("h1", "leased") == set()

    assert take(jobs, "h1:test:a", "w2", "t2", 300)
    assert jobs.reap(record, 300, 1) == "lost"

    lost = found(jobs, "h1:test:a")
    assert (lost.state, lost.outcome, lost.attempts) == ("lost", "lost", 1)
    assert jobs.token("h1:test:a") == ""
    assert jobs.members("h1", "done") == {"h1:test:a"}
    assert jobs.members("h1", "leased") == set()
    assert jobs.pending() == ["h1:test:b"]


def test_reap_allows_more_attempts(jobs: queue.Queue) -> None:
    jobs.enqueue([unit("a")], "h1", {})
    record = found(jobs, "h1:test:a")
    assert take(jobs, "h1:test:a")
    assert jobs.reap(record, 100, 2) == "pending"
    assert take(jobs, "h1:test:a")
    assert jobs.reap(record, 100, 2) == "pending"
    assert found(jobs, "h1:test:a").attempts == 2
    assert take(jobs, "h1:test:a")
    assert jobs.reap(record, 100, 2) == "lost"


def test_every_key_carries_the_lab_prefix(jobs: queue.Queue, client) -> None:
    jobs.enqueue([unit("a"), unit("b")], "h1", {})
    assert take(jobs, "h1:test:a")
    assert jobs.finish(found(jobs, "h1:test:a"), "tok", "passed")

    keys = set(client.keys("*"))

    assert keys == {
        "agentihooks-ci:queue:seq",
        "agentihooks-ci:queue:pending",
        "agentihooks-ci:queue:done",
        "agentihooks-ci:queue:head:h1:pending",
        "agentihooks-ci:queue:head:h1:done",
        "agentihooks-ci:queue:job:h1:test:a",
        "agentihooks-ci:queue:job:h1:test:b",
    }
    assert client.smembers("agentihooks-ci:queue:done") == {"h1:test:a"}


def test_missing_job_reads_none(jobs: queue.Queue) -> None:
    assert jobs.job("h1:test:none") is None
    assert jobs.token("h1:test:none") == ""
