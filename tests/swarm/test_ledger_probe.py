from http.client import IncompleteRead
from urllib.error import HTTPError

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import ledger_probe
from scripts.swarm.ledger_client import LedgerGone, LedgerRefused
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
    assert (sample.failure, sample.slow, ledger.writes) == ("timed out", True, [(None, None)])
    ledger.read_error = None
    ledger.write_error = SwarmError("ledger sw: ledger server not answering on http://127.0.0.1:8765: timed out")
    assert ledger_probe.measure(ledger, "sw", {}, clock).failure == "timed out"
    ledger.write_error = SwarmError("ledger sw: ledger server not answering: Connection refused")
    assert ledger_probe.measure(ledger, "sw", {}, clock).failure == "gave no answer"
    ledger.write_error = LedgerRefused("ledger sw refused: stale")
    refused = ledger_probe.measure(ledger, "sw", {}, clock)
    assert (refused.failure, refused.slow) == ("", False)
    ledger.write_error = LedgerGone("ledger sw does not exist")
    assert ledger_probe.measure(ledger, "sw", {}, clock).failure == ""
    ledger.write_error = SwarmError("ledger sw: server refused: 503 busy")
    assert ledger_probe.measure(ledger, "sw", {}, clock).failure == "answered with a server error"
    ledger.write_error = SwarmError("ledger sw: server refused: 409 stale")
    assert ledger_probe.measure(ledger, "sw", {}, clock).failure == "gave no answer"


@pytest.mark.parametrize(
    ("error", "failure"),
    [
        (HTTPError("http://ledger", 404, "missing", {}, None), ""),
        (HTTPError("http://ledger", 499, "client", {}, None), ""),
        (HTTPError("http://ledger", 500, "broken", {}, None), "answered with a server error"),
        (HTTPError("http://ledger", 503, "busy", {}, None), "answered with a server error"),
        (SystemExit("a remote ledger client needs a token"), "gave no answer"),
        (IncompleteRead(b"half"), "gave no answer"),
        (ConnectionResetError(), "gave no answer"),
        (ValueError("Expecting value: line 1 column 1"), "gave no answer"),
    ],
)
def test_a_read_failure_is_classified_in_plain_words(error, failure):
    clock = Clock()
    ledger = ProbedLedger(clock)
    ledger.read_error = error
    sample = ledger_probe.measure(ledger, "sw", {}, clock)
    assert (sample.failure, sample.slow, len(ledger.writes)) == (failure, bool(failure), 1)


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
    assert "Newest on dev merged 9 minutes ago: Run the cheap CI gates before every push." in mail
    assert mail.endswith("Idle and stale claim checks and nudges pause until two fast passes.")
    [note] = ledger.notes
    assert note.startswith("The ledger server is slow: two swarm passes in a row took read 6.0 seconds and write 7.5")
    assert "CPU 182 percent, started 74 minutes ago" in note and "pause" not in note
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
    ledger.write_error = SwarmError("ledger sw: ledger server not answering on http://127.0.0.1:8765: timed out")
    ledger.notify_error = OSError("connection refused")
    observe(store, ledger, clock)
    assert observe(store, ledger, clock) == ["raised the ledger slow alert"]
    [mail] = master_mail(store)
    assert "write 0.1 seconds, and the ledger timed out. Server CPU" in mail and ledger.notes == []
    assert "127.0.0.1" not in mail and "ledger sw" not in mail


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


def test_the_operator_notice_keeps_every_fact_within_the_panel_limit(store):
    clock = Clock()
    ledger = ProbedLedger(clock, read_s=12.25, write_s=20.5)
    ledger.write_error = TimeoutError("timed out")
    subject = "Keep a merge wait queued while a fresh queue entry is not yet visible " * 4
    for _ in range(2):
        ledger_probe.observe(
            store, "sw", ledger, FakeRuntime(), 1_000, clock=clock, facts=lambda: {**FACTS, "newest": subject}
        )
    [note] = ledger.notes
    assert len(note) == ledger_probe.TEXT_KEPT
    for words in ("read 12.2 seconds", "write 20.5 seconds", "timed out", "CPU 182 percent", "started 74 minutes ago"):
        assert words in note
    assert "Newest on dev merged 9 minutes ago: Keep a merge wait" in note


def test_unknown_server_facts_are_named_unknown(store):
    clock = Clock()
    ledger = ProbedLedger(clock, read_s=6.0)
    unknown = {"cpu": None, "started_minutes": None, "newest": None, "merged_minutes": None}
    for _ in range(2):
        ledger_probe.observe(store, "sw", ledger, FakeRuntime(), 1_000, clock=clock, facts=lambda: unknown)
    [mail] = master_mail(store)
    assert "Server CPU unknown, start time unknown. Newest on dev unknown." in mail


def test_a_cpu_reading_is_shown_in_whole_percent(store):
    clock = Clock()
    ledger = ProbedLedger(clock, read_s=6.0)
    for _ in range(2):
        ledger_probe.observe(store, "sw", ledger, FakeRuntime(), 1_000, clock=clock, facts=lambda: {**FACTS, "cpu": 0})
    assert "Server CPU 0 percent, started 74 minutes ago." in master_mail(store)[0]


@pytest.mark.parametrize("error", [LedgerRefused("ledger sw refused: not plain words"), LedgerGone("gone")])
def test_a_notice_the_ledger_refuses_is_dropped_so_later_notices_still_land(store, error):
    clock = Clock()
    ledger = ProbedLedger(clock, read_s=6.0)
    ledger.notify_error = error
    observe(store, ledger, clock)
    observe(store, ledger, clock)
    assert ledger_probe.state(store, "sw")["notices"] == []
    ledger.notify_error, ledger.read_s = None, 0.1
    observe(store, ledger, clock)
    observe(store, ledger, clock)
    [note] = ledger.notes
    assert note.startswith("The ledger server answers fast again")


def test_the_ledger_client_reads_the_metadata_resource(monkeypatch):
    from scripts.swarm.ledger_client import LedgerClient

    seen = []
    monkeypatch.setattr(LedgerClient, "_resource", lambda self, slug, path: seen.append((slug, path)) or {"a": 1})
    assert LedgerClient().metadata("sw") == {"a": 1} and seen == [("sw", "metadata")]


def test_a_ledger_missing_either_probe_call_is_not_measured():
    class ReadOnly(FakeLedger):
        def metadata(self, slug):
            return {}

    class WriteOnly(FakeLedger):
        def time_left(self, slug, slots, ci_minutes):
            return None

    assert ledger_probe.measure(ReadOnly([]), "sw", {}, Clock()) is None
    assert ledger_probe.measure(WriteOnly([]), "sw", {}, Clock()) is None


def test_the_probe_writes_the_inputs_the_time_left_pass_would_send(store, monkeypatch):
    seen, runtime = [], FakeRuntime()
    monkeypatch.setattr(
        ledger_probe.time_left,
        "inputs_of",
        lambda s, slug, rt: seen.append((slug, rt)) or {"slots": 4, "ci_minutes": 2.5},
    )
    clock = Clock()
    ledger = ProbedLedger(clock)
    ledger_probe.observe(store, "sw", ledger, runtime, 1_000, clock=clock, facts=lambda: FACTS)
    assert seen == [("sw", runtime)] and ledger.writes == [(4, 2.5)]


def test_the_alert_state_is_kept_under_the_swarm_key(store):
    clock = Clock()
    ledger = ProbedLedger(clock, read_s=0.2)
    observe(store, ledger, clock)
    assert ledger_probe.state(store, "sw") == {"slow": 0, "fast": 1, "notices": []}
    assert store.redis.get(store.key("sw", "ledger-slow")) == '{"slow": 0, "fast": 1, "notices": []}'
    ledger.read_s = 6.0
    observe(store, ledger, clock, at=2_000)
    observe(store, ledger, clock, at=3_000)
    assert ledger_probe.state(store, "sw") == {"slow": 2, "fast": 0, "alert": True, "raised_at": 3_000, "notices": []}
    ledger.read_s = 0.2
    observe(store, ledger, clock)
    observe(store, ledger, clock)
    assert ledger_probe.state(store, "sw") == {"slow": 0, "fast": 2, "alert": False, "raised_at": 3_000, "notices": []}


def test_the_master_is_told_first_and_the_clear_is_for_its_awareness(store):
    clock = Clock()
    ledger = ProbedLedger(clock, read_s=6.0)
    observe(store, ledger, clock)
    observe(store, ledger, clock)
    ledger.read_s = 0.2
    observe(store, ledger, clock)
    observe(store, ledger, clock)
    items = InboxStore(store.redis).pending_items("master@sw")
    assert [(i.sender, i.fyi) for i in items] == [("swarm", False), ("swarm", True)]
    assert items[1].text == (
        "The ledger server answers fast again: two swarm passes took read 0.2 seconds and write 0.1 seconds. "
        "Idle and stale claim checks resume."
    )


def test_a_master_without_a_seat_gets_the_alert_at_its_name():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    store.put_agent("sw", AgentRecord("master@a1b2c3-0001", "master", ""))
    clock = Clock()
    ledger = ProbedLedger(clock, read_s=6.0)
    observe(store, ledger, clock)
    observe(store, ledger, clock)
    assert len(InboxStore(store.redis).pending_items("master@a1b2c3-0001")) == 1
    assert InboxStore(store.redis).pending_items("master@sw") == []


def test_notices_held_while_the_ledger_is_down_land_in_order(store):
    clock = Clock()
    ledger = ProbedLedger(clock, read_s=6.0)
    ledger.notify_error = OSError("connection refused")
    observe(store, ledger, clock)
    observe(store, ledger, clock)
    ledger.read_s = 0.2
    observe(store, ledger, clock)
    observe(store, ledger, clock)
    held = ledger_probe.state(store, "sw")["notices"]
    assert [n.split(":")[0] for n in held] == ["The ledger server is slow", "The ledger server answers fast again"]
    ledger.notify_error = None
    observe(store, ledger, clock)
    assert ledger.notes == held and ledger_probe.state(store, "sw")["notices"] == []


def test_a_dropped_notice_is_reported_on_stderr(store, capsys):
    clock = Clock()
    ledger = ProbedLedger(clock, read_s=6.0)
    ledger.notify_error = LedgerRefused("ledger sw refused: not plain words")
    observe(store, ledger, clock)
    observe(store, ledger, clock)
    captured = capsys.readouterr()
    assert captured.err.endswith("ledger slow notice dropped: ledger sw refused: not plain words\n")
    assert "dropped" not in captured.out


def test_a_finding_with_a_verdict_returns_only_after_its_cooldown(store, monkeypatch):
    from scripts.swarm import status
    from scripts.swarm.health.findings import Finding

    measure = {"n": 4}
    monkeypatch.setattr(
        status.health,
        "findings",
        lambda *a, **k: [Finding("ceremony", "engineer@a1b2c3-0002", "talk", (), "12 per outcome", measure["n"])],
    )
    for name in ("live_binding", "retire_watch", "launch_check", "spawn_stall", "drain_watch"):
        monkeypatch.setattr(f"scripts.swarm.status.{name}.findings", lambda *a: [])
    at = {"now": 10_000_000}
    monkeypatch.setattr(status, "now_ms", lambda: at["now"])
    [shown] = status.findings(store, "sw", store.config("sw"), [], [])
    assert shown["seen_at"] == 10_000_000
    status.verdict_store(store, "sw").judge(shown["id"], "early-real", "watching", "master", 10_000_000)
    measure["n"] = 9
    cooldown = status.health.limits().cooldown_minutes * 60_000
    at["now"] = 10_000_000 + cooldown - 1
    assert status.findings(store, "sw", store.config("sw"), [], []) == []
    at["now"] = 10_000_000 + cooldown
    assert [f["kind"] for f in status.findings(store, "sw", store.config("sw"), [], [])] == ["ceremony"]


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
            self.written = (slots, ci_minutes)

    ledger, runtime = TimingOut([]), FakeRuntime()
    cli.run_tick(store, "sw", ledger, runtime, FakeHerdr({}))
    assert not ledger_probe.holding(store, "sw")
    actions = cli.run_tick(store, "sw", ledger, runtime, FakeHerdr({}))
    assert "raised the ledger slow alert" in actions
    assert any("timed out" in text for text in master_mail(store)) and ledger.written == (None, None)
