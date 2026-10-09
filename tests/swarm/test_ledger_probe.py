import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import ledger_probe
from scripts.swarm.ledger_client import LedgerRefused
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")

FACTS = {"cpu": 182.0, "started_minutes": 74, "newest": "Run the cheap CI gates before every push", "merged_minutes": 9}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ProbedLedger(FakeLedger):
    def __init__(self, clock, read_s=0.1, write_s=0.1):
        super().__init__([])
        self.clock, self.read_s, self.write_s = clock, read_s, write_s
        self.read_error = self.write_error = self.notify_error = None
        self.reads, self.writes = 0, []

    def metadata(self, slug):
        assert slug == "sw"
        self.reads += 1
        self.clock.now += self.read_s
        if self.read_error:
            raise self.read_error
        return {"title": "Swarm"}

    def time_left(self, slug, slots, ci_minutes):
        assert slug == "sw"
        self.writes.append((slots, ci_minutes))
        self.clock.now += self.write_s
        if self.write_error:
            raise self.write_error

    def notify(self, slug, text):
        if self.notify_error:
            raise self.notify_error
        super().notify(slug, text)


@pytest.fixture
def store():
    import fakeredis

    s = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    s.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    s.put_agent("sw", AgentRecord("master@a1b2c3-0001", "master", "", seat="master@sw"))
    return s


def master_mail(store):
    return [i.text for i in InboxStore(store.redis).pending_items("master@sw")]


def observe(store, ledger, clock, at=1_000):
    return ledger_probe.observe(store, "sw", ledger, FakeRuntime(), at, clock=clock, facts=lambda: FACTS)


def test_each_pass_times_one_read_and_one_write():
    clock = Clock()
    ledger = ProbedLedger(clock, read_s=1.5, write_s=2.25)
    sample = ledger_probe.measure(ledger, "sw", {"slots": 3, "ci_minutes": 12.0}, clock)
    assert (ledger.reads, ledger.writes) == (1, [(3, 12.0)])
    assert (sample.read_s, sample.write_s, sample.failure, sample.slow) == (1.5, 2.25, "", False)


def test_a_read_or_write_over_five_seconds_is_slow():
    clock = Clock()
    assert ledger_probe.measure(ProbedLedger(clock, read_s=5.5), "sw", {}, clock).slow
    assert ledger_probe.measure(ProbedLedger(clock, write_s=5.5), "sw", {}, clock).slow
    assert not ledger_probe.measure(ProbedLedger(clock, read_s=5.0, write_s=5.0), "sw", {}, clock).slow


def test_a_timeout_or_no_answer_is_a_failed_pass_and_a_refusal_is_an_answer():
    clock = Clock()
    ledger = ProbedLedger(clock)
    ledger.read_error = TimeoutError("timed out")
    sample = ledger_probe.measure(ledger, "sw", {}, clock)
    assert sample.slow and "timed out" in sample.failure and ledger.writes == [(None, None)]
    ledger.read_error, ledger.write_error = None, SwarmError("ledger sw: ledger server not answering: timed out")
    assert "not answering" in ledger_probe.measure(ledger, "sw", {}, clock).failure
    ledger.write_error = LedgerRefused("ledger sw refused: stale")
    refused = ledger_probe.measure(ledger, "sw", {}, clock)
    assert (refused.failure, refused.slow) == ("", False)


def test_a_ledger_without_the_probe_calls_is_not_measured(store):
    assert ledger_probe.measure(FakeLedger([]), "sw", {}, Clock()) is None
    assert ledger_probe.observe(store, "sw", FakeLedger([]), FakeRuntime(), 1_000) == []
    assert not ledger_probe.holding(store, "sw")


def test_one_slow_pass_raises_nothing_and_two_in_a_row_raise_the_alert(store):
    clock = Clock()
    ledger = ProbedLedger(clock, read_s=6.0, write_s=7.5)
    assert observe(store, ledger, clock) == []
    assert not ledger_probe.holding(store, "sw") and master_mail(store) == [] and ledger.notes == []
    assert observe(store, ledger, clock) == ["raised the ledger slow alert"]
    assert ledger_probe.holding(store, "sw")
    [mail] = master_mail(store)
    for words in ("read 6.0 seconds", "write 7.5 seconds", "CPU 182 percent", "started 74 minutes ago"):
        assert words in mail
    assert "Run the cheap CI gates before every push, merged to dev 9 minutes ago" in mail
    assert ledger.notes and "read 6.0 seconds" in ledger.notes[0]
    assert observe(store, ledger, clock) == [] and len(master_mail(store)) == 1 and len(ledger.notes) == 1


def test_a_fast_pass_between_slow_ones_resets_the_count(store):
    clock = Clock()
    ledger = ProbedLedger(clock, read_s=6.0)
    observe(store, ledger, clock)
    ledger.read_s = 0.1
    observe(store, ledger, clock)
    ledger.read_s = 6.0
    assert observe(store, ledger, clock) == []
    assert not ledger_probe.holding(store, "sw")


def test_two_failed_passes_raise_the_alert_naming_the_failure(store):
    clock = Clock()
    ledger = ProbedLedger(clock)
    ledger.read_error = TimeoutError("timed out")
    ledger.write_error = SwarmError("ledger sw: ledger server not answering: timed out")
    ledger.notify_error = SwarmError("ledger sw: ledger server not answering: timed out")
    observe(store, ledger, clock)
    assert observe(store, ledger, clock) == ["raised the ledger slow alert"]
    [mail] = master_mail(store)
    assert "timed out" in mail and ledger.notes == []


def test_the_operator_notice_is_retried_each_pass_until_the_ledger_takes_it(store):
    clock = Clock()
    ledger = ProbedLedger(clock, read_s=9.0)
    ledger.notify_error = SwarmError("ledger sw: ledger server not answering: timed out")
    observe(store, ledger, clock)
    observe(store, ledger, clock)
    observe(store, ledger, clock)
    assert ledger.notes == []
    ledger.notify_error = None
    observe(store, ledger, clock)
    assert len(ledger.notes) == 1 and "read 9.0 seconds" in ledger.notes[0]
    observe(store, ledger, clock)
    assert len(ledger.notes) == 1


def test_two_fast_passes_clear_the_alert_and_tell_the_master_and_operator(store):
    clock = Clock()
    ledger = ProbedLedger(clock, read_s=6.0)
    observe(store, ledger, clock)
    observe(store, ledger, clock)
    ledger.read_s = 0.2
    assert observe(store, ledger, clock) == []
    assert ledger_probe.holding(store, "sw")
    assert observe(store, ledger, clock) == ["cleared the ledger slow alert"]
    assert not ledger_probe.holding(store, "sw")
    assert "answers fast again" in master_mail(store)[-1]
    assert "answers fast again" in ledger.notes[-1]


def test_the_alert_goes_to_the_master_seat_when_no_master_is_live():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    clock = Clock()
    ledger = ProbedLedger(clock, read_s=6.0)
    observe(store, ledger, clock)
    observe(store, ledger, clock)
    assert len(master_mail(store)) == 1


def test_the_operator_notice_is_in_plain_words(store):
    clock = Clock()
    ledger = ProbedLedger(clock, read_s=6.0)
    observe(store, ledger, clock)
    observe(store, ledger, clock)
    from scripts.swarm_ledger import ledger_comments

    assert not any(pattern.search(ledger.notes[0]) for _, pattern in ledger_comments.RULES)


def raise_alert(store):
    clock = Clock()
    ledger = ProbedLedger(clock, read_s=6.0)
    observe(store, ledger, clock)
    observe(store, ledger, clock)
    assert ledger_probe.holding(store, "sw")
    return ledger, clock


def worker(store, name):
    return next(a for a in store.agents("sw") if a.name == name)


def test_idle_ticks_and_nudges_pause_while_the_alert_holds(store):
    from scripts.swarm.tick import IDLE_NUDGE_TICKS, _watch_idle

    name = "engineer@a1b2c3-0002"
    store.put_agent("sw", AgentRecord(name, "eng", "t1", started_at=1_000))
    ledger, clock = raise_alert(store)
    runtime = FakeRuntime()
    runtime.statuses[name] = "idle"
    for n in range(IDLE_NUDGE_TICKS + 2):
        assert _watch_idle("sw", store, ledger, runtime, {}, worker(store, name), 2_000 + n) == []
    assert runtime.nudged == [] and worker(store, name).idle_ticks == 0
    ledger.read_s = 0.1
    observe(store, ledger, clock)
    observe(store, ledger, clock)
    for n in range(IDLE_NUDGE_TICKS):
        _watch_idle("sw", store, ledger, runtime, {}, worker(store, name), 3_000 + n)
    assert runtime.nudged == [name]


def test_idle_and_stale_claim_findings_pause_while_the_alert_holds(store, monkeypatch):
    from scripts.swarm import status
    from scripts.swarm.health.findings import Finding

    shown = [
        Finding("idle with claim", "engineer@a1b2c3-0002", "idle", (), "3 idle ticks", 4),
        Finding("stale claim", "t1", "quiet", (), "30 minutes", 40),
        Finding("ceremony", "engineer@a1b2c3-0002", "talk", (), "12 per outcome", 20),
    ]
    monkeypatch.setattr(status.health, "findings", lambda *a, **k: list(shown))
    for name in ("live_binding", "retire_watch", "launch_check", "spawn_stall", "drain_watch"):
        monkeypatch.setattr(f"scripts.swarm.status.{name}.findings", lambda *a: [])

    def kinds():
        return [f["kind"] for f in status.findings(store, "sw", store.config("sw"), [], [])]

    assert kinds() == ["idle with claim", "stale claim", "ceremony"]
    raise_alert(store)
    assert kinds() == ["ceremony"]


def test_the_swarm_timer_pass_runs_the_probe(store, monkeypatch):
    from scripts.swarm import cli, ledger_host

    monkeypatch.setattr(ledger_host, "facts", lambda: FACTS)
    from tests.swarm.test_delivery import FakeHerdr

    class TimingOut(FakeLedger):
        def metadata(self, slug):
            raise TimeoutError("timed out")

        def time_left(self, slug, slots, ci_minutes):
            raise SwarmError("ledger sw: ledger server not answering: timed out")

    ledger, runtime = TimingOut([]), FakeRuntime()
    cli.run_tick(store, "sw", ledger, runtime, FakeHerdr({}))
    assert not ledger_probe.holding(store, "sw")
    actions = cli.run_tick(store, "sw", ledger, runtime, FakeHerdr({}))
    assert "raised the ledger slow alert" in actions
    assert any("timed out" in text for text in master_mail(store))
