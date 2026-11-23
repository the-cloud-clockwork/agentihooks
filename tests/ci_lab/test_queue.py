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


@pytest.mark.feature("lab-worker-executors")
def test_enqueue_stores_the_base_and_grader_revision_each_job_reads(jobs: queue.Queue) -> None:
    jobs.enqueue([unit("a")], "h1", {}, "b1", "g1")
    jobs.enqueue([unit("c")], "h2", {})

    assert (found(jobs, "h1:test:a").base, found(jobs, "h1:test:a").grader) == ("b1", "g1")
    assert (found(jobs, "h2:test:c").base, found(jobs, "h2:test:c").grader) == ("", "")


def take(
    jobs: queue.Queue, job: str, worker: str = "w1", token: str = "tok", expires_ms: int = 100, leased_ms: int = 0
) -> bool:
    return jobs.claim(found(jobs, job), worker, token, expires_ms, leased_ms)


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

    assert jobs.claim(record, "w1", "t1", 100, 0) is True
    assert jobs.claim(record, "w2", "t2", 100, 0) is False
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


@pytest.mark.feature("lab-scheduler-metrics")
def test_claim_records_when_the_lease_started_and_an_older_lease_reads_zero(jobs: queue.Queue, client) -> None:
    jobs.enqueue([unit("a"), unit("b")], "h1", {})
    assert found(jobs, "h1:test:a").leased_ms == 0
    assert take(jobs, "h1:test:a", expires_ms=1100, leased_ms=1000)
    assert found(jobs, "h1:test:a").leased_ms == 1000
    assert take(jobs, "h1:test:b", expires_ms=1100, leased_ms=1000)
    client.hdel("agentihooks-ci:queue:job:h1:test:b", "leased_ms")
    assert found(jobs, "h1:test:b").leased_ms == 0


@pytest.mark.feature("lab-scheduler-metrics")
def test_tally_counts_each_finished_attempt_of_a_job_once(jobs: queue.Queue) -> None:
    jobs.enqueue([unit("a"), unit("b")], "h1", {})
    assert take(jobs, "h1:test:a")
    assert jobs.finish(found(jobs, "h1:test:a"), "tok", "crash")
    first = found(jobs, "h1:test:a")
    assert jobs.tally(first) is True
    assert jobs.tally(first) is False
    assert jobs.retry(first)
    assert take(jobs, "h1:test:a", "w2", "t2")
    assert jobs.finish(found(jobs, "h1:test:a"), "t2", "passed")
    second = found(jobs, "h1:test:a")
    assert second.attempts == 1
    assert jobs.tally(second) is True
    assert jobs.tally(second) is False
    assert jobs.tally(first) is False
    assert take(jobs, "h1:test:b")
    assert jobs.finish(found(jobs, "h1:test:b"), "tok", "passed")
    assert jobs.tally(found(jobs, "h1:test:b")) is True


def test_expired_includes_a_lease_at_time_zero(jobs: queue.Queue) -> None:
    jobs.enqueue([unit("a")], "h1", {})
    assert take(jobs, "h1:test:a", expires_ms=0)

    assert jobs.expired(0) == ["h1:test:a"]


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
    assert jobs.client.smembers("agentihooks-ci:queue:done") == {"h1:test:a"}
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


@pytest.mark.feature("lab-scheduler-test-units")
def test_retry_moves_a_finished_job_back_to_pending_with_one_more_attempt(jobs: queue.Queue) -> None:
    jobs.enqueue([unit("a"), unit("b")], "h1", {})
    assert take(jobs, "h1:test:a")
    record = found(jobs, "h1:test:a")
    assert jobs.retry(record) is False
    assert jobs.finish(record, "tok", "crash")

    assert jobs.retry(record) is True
    again = found(jobs, "h1:test:a")
    assert (again.state, again.outcome, again.attempts, again.worker) == ("pending", "", 1, "")
    assert jobs.token("h1:test:a") == ""
    assert jobs.pending() == ["h1:test:b", "h1:test:a"]
    assert jobs.members("h1", "pending") == {"h1:test:a", "h1:test:b"}
    assert jobs.members("h1", "done") == set()
    assert jobs.client.smembers("agentihooks-ci:queue:done") == set()
    assert jobs.retry(record) is False

    assert take(jobs, "h1:test:a", "w2", "t2")
    assert jobs.finish(found(jobs, "h1:test:a"), "t2", "crash")
    assert jobs.retry(record) is True
    assert found(jobs, "h1:test:a").attempts == 2


@pytest.mark.feature("lab-lease-class-order")
def test_a_more_urgent_rank_sorts_ahead_of_work_enqueued_earlier(jobs: queue.Queue) -> None:
    jobs.enqueue([unit("q1"), unit("q2")], "merge", {}, rank=2)
    jobs.enqueue([unit("b")], "sweep", {}, rank=4)
    jobs.enqueue([unit("p1"), unit("p2")], "pull", {}, rank=1)
    jobs.enqueue([unit("x")], "fix", {}, rank=0)
    jobs.enqueue([unit("q3")], "merge", {}, rank=2)

    assert jobs.pending() == [
        "fix:test:x",
        "pull:test:p1",
        "pull:test:p2",
        "merge:test:q1",
        "merge:test:q2",
        "merge:test:q3",
        "sweep:test:b",
    ]
    assert [found(jobs, job).rank for job in ("fix:test:x", "pull:test:p1", "merge:test:q3", "sweep:test:b")] == [
        0,
        1,
        2,
        4,
    ]


@pytest.mark.feature("lab-lease-class-order")
def test_an_unranked_job_keeps_its_counter_score_and_sorts_after_every_ranked_one(jobs: queue.Queue) -> None:
    jobs.enqueue([unit("old")], "h1", {})
    jobs.enqueue([unit("sweep")], "h2", {}, rank=queue.RANKS - 1)

    assert jobs.pending() == ["h2:test:sweep", "h1:test:old"]
    assert jobs.client.zscore(f"{queue.PREFIX}pending", "h1:test:old") == 1
    assert jobs.client.zscore(f"{queue.PREFIX}pending", "h2:test:sweep") == 2 - queue.BAND
    assert found(jobs, "h1:test:old").rank == queue.UNRANKED == queue.RANKS


@pytest.mark.feature("lab-lease-class-order")
def test_a_job_enqueued_before_ranks_existed_sorts_after_ranked_work_and_moves_under_a_rank(jobs: queue.Queue) -> None:
    jobs.enqueue([unit("old"), unit("kept")], "h1", {})
    for job in ("h1:test:old", "h1:test:kept"):
        jobs.client.hdel(f"{queue.PREFIX}job:{job}", "rank")
    jobs.enqueue([unit("p")], "pull", {}, rank=1)

    assert found(jobs, "h1:test:old").rank == queue.UNRANKED
    assert list(jobs.ordered()) == ["pull:test:p", "h1:test:old", "h1:test:kept"]

    assert jobs.enqueue([unit("kept")], "h1", {}, rank=0) == []

    assert list(jobs.ordered()) == ["h1:test:kept", "pull:test:p", "h1:test:old"]
    assert jobs.client.zscore(f"{queue.PREFIX}pending", "h1:test:kept") == 2 - queue.RANKS * queue.BAND
    assert found(jobs, "h1:test:kept").rank == 0


@pytest.mark.feature("lab-lease-class-order")
def test_a_reaped_or_retried_job_keeps_its_rank(jobs: queue.Queue) -> None:
    jobs.enqueue([unit("a"), unit("b")], "pull", {}, rank=1)
    jobs.enqueue([unit("q")], "merge", {}, rank=2)
    assert take(jobs, "pull:test:a")

    assert jobs.reap(found(jobs, "pull:test:a"), 100, 1) == "pending"

    assert jobs.pending() == ["pull:test:b", "pull:test:a", "merge:test:q"]
    assert take(jobs, "pull:test:b")
    record = found(jobs, "pull:test:b")
    assert jobs.finish(record, "tok", "crash")

    assert jobs.retry(record) is True

    assert jobs.pending() == ["pull:test:a", "pull:test:b", "merge:test:q"]
    assert (found(jobs, "pull:test:a").rank, found(jobs, "pull:test:b").rank) == (1, 1)


@pytest.mark.feature("lab-lease-class-order")
def test_a_more_urgent_enqueue_moves_a_pending_job_and_leaves_a_leased_one(jobs: queue.Queue) -> None:
    jobs.enqueue([unit("a"), unit("b"), unit("c")], "h1", {}, rank=3)
    jobs.enqueue([unit("p")], "pull", {}, rank=1)
    assert take(jobs, "h1:test:a")

    assert jobs.enqueue([unit("a"), unit("c")], "h1", {}, rank=1) == []

    assert jobs.pending() == ["h1:test:c", "pull:test:p", "h1:test:b"]
    assert (found(jobs, "h1:test:c").rank, found(jobs, "h1:test:b").rank) == (1, 3)
    leased = found(jobs, "h1:test:a")
    assert (leased.state, leased.rank) == ("leased", 3)
    assert jobs.members("h1", "pending") == {"h1:test:b", "h1:test:c"}

    assert jobs.enqueue([unit("c")], "h1", {}, rank=4) == []

    assert jobs.pending() == ["h1:test:c", "pull:test:p", "h1:test:b"]
    assert found(jobs, "h1:test:c").rank == 1


@pytest.mark.feature("lab-lease-class-order")
def test_a_more_urgent_enqueue_leaves_a_finished_job_alone(jobs: queue.Queue) -> None:
    jobs.enqueue([unit("a")], "h1", {}, rank=3)
    assert take(jobs, "h1:test:a")
    assert jobs.finish(found(jobs, "h1:test:a"), "tok", "crash")

    assert jobs.enqueue([unit("a")], "h1", {}, rank=1) == []

    finished = found(jobs, "h1:test:a")
    assert (finished.state, finished.rank) == ("done", 3)
    assert jobs.pending() == []


@pytest.mark.feature("lab-lease-class-order")
def test_the_lease_order_is_read_in_pages_that_a_claim_between_them_cannot_shift(
    jobs: queue.Queue, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs.enqueue([unit(str(number)) for number in range(7)], "h1", {}, rank=1)
    pages: list[tuple[object, ...]] = []
    read = jobs.client.zrangebyscore

    def counted(*arguments: object, **options: object) -> object:
        pages.append(arguments)
        return read(*arguments, **options)

    monkeypatch.setattr(jobs.client, "zrangebyscore", counted)
    ordered = jobs.ordered(page=3)

    assert next(ordered) == "h1:test:0"
    assert len(pages) == 1
    assert take(jobs, "h1:test:0")
    assert list(ordered) == [f"h1:test:{number}" for number in range(1, 7)]
    assert len(pages) == 3
    assert list(jobs.ordered(page=7)) == [f"h1:test:{number}" for number in range(1, 7)]


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


@pytest.mark.feature("lab-scheduler-service")
def test_leased_lists_each_leased_job_and_offers_keep_each_workers_latest_free_slots(jobs: queue.Queue) -> None:
    ids = jobs.enqueue([unit("a"), unit("b"), unit("c")], "h1", {})
    assert jobs.leased() == [] and jobs.offers(0) == {}
    assert jobs.claim(found(jobs, ids[2]), "w1", "t1", 900, 0)
    assert jobs.claim(found(jobs, ids[0]), "w1", "t2", 500, 0)
    assert jobs.claim(found(jobs, ids[1]), "w2", "t3", 700, 0)
    assert jobs.leased() == [ids[0], ids[1], ids[2]]
    assert jobs.finish(found(jobs, ids[0]), "t2", "passed")
    assert jobs.leased() == [ids[1], ids[2]]
    jobs.offer("w1", 3, 1000)
    jobs.offer("w2", 0, 1100)
    jobs.offer("w1", 1, 1200)
    assert jobs.offers(1100) == {"w1": (1, 1200), "w2": (0, 1100)}
    assert jobs.offers(1101) == {"w1": (1, 1200)}
    assert jobs.offers(0) == {"w1": (1, 1200), "w2": (0, 1100)}
    assert jobs.prune(1100) == 0
    assert jobs.prune(1101) == 1
    assert jobs.offers(0) == {"w1": (1, 1200)}
    jobs.offer("w3", 2, 900)
    jobs.offer("w3", 5, 1300)
    assert jobs.prune(1201) == 1
    assert jobs.offers(0) == {"w3": (5, 1300)}


@pytest.mark.feature("lab-scheduler-pass-moves-changed-grades")
def test_states_reads_each_job_state_in_order_through_plain_pipelines_and_a_missing_job_reads_none(
    jobs: queue.Queue, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = jobs.enqueue([unit("a"), unit("b"), unit("c")], "h1", {})
    assert jobs.claim(found(jobs, ids[1]), "w1", "t1", 900, 0)
    assert jobs.claim(found(jobs, ids[2]), "w1", "t2", 900, 0)
    assert jobs.finish(found(jobs, ids[2]), "t2", "passed")
    opened: list[tuple[tuple[object, ...], dict[str, object]]] = []
    pipeline = jobs.client.pipeline

    def piped(*args: object, **options: object) -> object:
        opened.append((args, options))
        return pipeline(*args, **options)

    monkeypatch.setattr(jobs.client, "pipeline", piped)
    assert jobs.states([ids[2], "h1:test:none", ids[0], ids[1]]) == ["done", None, "pending", "leased"]
    assert jobs.states([]) == []
    assert opened == [((), {"transaction": False})] * 2
