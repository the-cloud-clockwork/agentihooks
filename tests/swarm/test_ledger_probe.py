from http.client import IncompleteRead
from urllib.error import HTTPError

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import incidents, ledger_probe
from scripts.swarm.ledger_client import LedgerGone, LedgerRefused
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from tests.swarm.test_tick import FakeLedger, FakeRuntime

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
    assert mail == (
        "The ledger server is slow: sw pass 1: read 6.0 seconds and write 7.5 seconds, "
        "CPU unknown over pass, streaks before/after slow 0/1, fast 0/0. "
        "sw pass 2: read 6.0 seconds and write 7.5 seconds, "
        "CPU unknown over pass, streaks before/after slow 1/2, fast 0/0. "
        "Server started 74 minutes ago. Newest on dev merged 9 minutes ago: "
        "Run the cheap CI gates before every push. "
        "Idle and stale claim checks and nudges pause until recovery below four seconds."
    )
    for words in ("read 6.0 seconds", "write 7.5 seconds", "CPU unknown over pass", "started 74 minutes ago"):
        assert words in mail
    assert "Newest on dev merged 9 minutes ago: Run the cheap CI gates before every push." in mail
    assert mail.endswith("Idle and stale claim checks and nudges pause until recovery below four seconds.")
    [note] = ledger.notes
    assert note.startswith("The ledger server is slow: sw pass 1: read 6.0 seconds and write 7.5")
    assert "CPU unknown over pass" in note and "Server started 74 minutes ago" in note and "pause" not in note
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


@pytest.mark.parametrize("sibling_passes", [1, 2])
def test_a_sibling_swarm_cannot_reset_or_clear_another_swarms_slow_passes(store, sibling_passes):
    store.create(SwarmConfig("sibling", "/repo", max_eng=1, max_ci=0))
    clock = Clock()
    slow = ProbedLedger(clock, write_s=5.7)
    fast = ProbedLedger(clock)
    read, write = fast.metadata, fast.time_left
    fast.metadata = lambda slug: read("sw")
    fast.time_left = lambda slug, slots, ci_minutes: write("sw", slots, ci_minutes)
    observe(store, slow, clock)
    for at in range(sibling_passes):
        ledger_probe.observe(store, "sibling", fast, FakeRuntime(), 2_000 + at, clock=clock, facts=lambda: FACTS)
    observe(store, slow, clock, 3_000)
    assert ledger_probe.holding(store, "sw")
    assert len(master_mail(store)) == 1
    assert "slow" in master_mail(store)[0]
    for at in range(2):
        ledger_probe.observe(store, "sibling", fast, FakeRuntime(), 4_000 + at, clock=clock, facts=lambda: FACTS)
    assert store.redis.hget(incidents.key("ledger"), "active") == "1"
    observe(store, slow, clock, 5_000)
    assert not any("fast again" in text for text in master_mail(store))


def test_notices_keep_both_triggering_passes_and_do_not_substitute_a_new_swarms_fast_sample(store):
    store.create(SwarmConfig("sibling", "/repo", max_eng=1, max_ci=0))
    clock = Clock()
    ledger = ProbedLedger(clock, read_s=6.2, write_s=0.7)
    ledger.read_error = TimeoutError("timed out")
    observe(store, ledger, clock, 1_000)
    ledger.read_s, ledger.write_s = 0.2, 7.8
    ledger.read_error, ledger.write_error = None, HTTPError("url", 503, "unavailable", {}, None)
    observe(store, ledger, clock, 2_000)
    [notice] = master_mail(store)
    for words in ("read 6.2", "write 0.7", "timed out", "read 0.2", "write 7.8", "server error"):
        assert words in notice
        assert words in ledger.notes[0]
    first_notice = notice
    ledger.read_s, ledger.write_s, ledger.write_error = 0.1, 0.1, None
    read, write = ledger.metadata, ledger.time_left
    ledger.metadata = lambda slug: read("sw")
    ledger.time_left = lambda slug, slots, ci_minutes: write("sw", slots, ci_minutes)
    ledger_probe.observe(store, "sibling", ledger, FakeRuntime(), 3_000, clock=clock, facts=lambda: FACTS)
    sibling_notice = InboxStore(store.redis).pending_items("master@sibling")[0].text
    assert sibling_notice == first_notice
    held = ledger_probe.state(store, "sw")
    assert [row["sequence"] for row in held["raised_samples"]] == [1, 2]
    assert [row["at"] for row in held["raised_samples"]] == [1_000, 2_000]
    assert [row["slug"] for row in held["raised_samples"]] == ["sw", "sw"]
    assert held["raised_samples"][0]["read_failure"] == "timed out"
    assert held["raised_samples"][1]["write_failure"] == "answered with a server error"
    assert held["raised_samples"][0]["streak_before"] == {"slow": 0, "fast": 0}
    assert held["raised_samples"][1]["streak_after"] == {"slow": 2, "fast": 0}


def test_triggering_evidence_stays_fixed_while_the_current_pass_window_advances(store):
    clock = Clock()
    ledger = ProbedLedger(clock)
    for sequence, latency in enumerate((6.1, 6.2, 6.3, 6.4), 1):
        ledger.read_s = latency
        observe(store, ledger, clock, sequence * 1_000)
    held = ledger_probe.state(store, "sw")
    assert [row["sequence"] for row in held["raised_samples"]] == [1, 2]
    assert [row["read_s"] for row in held["raised_samples"]] == pytest.approx([6.1, 6.2])
    assert [row["sequence"] for row in held["samples"]] == [3, 4]
    assert [row["read_s"] for row in held["samples"]] == pytest.approx([6.3, 6.4])
    [notice] = master_mail(store)
    assert "read 6.1 seconds" in notice and "read 6.2 seconds" in notice
    assert "read 6.3 seconds" not in notice and "read 6.4 seconds" not in notice


def test_notice_streaks_and_cpu_survive_long_watchdog_details(store):
    clock = Clock()
    ledger = ProbedLedger(clock)
    reason = "The ledger server restart failed: " + "x" * 200
    store.redis.hset(incidents.key("ledger"), "watchdog", reason)
    observe(store, ledger, clock, 1_000)
    observe(store, ledger, clock, 2_000)
    [notice] = ledger.notes
    assert len(notice) <= ledger_probe.TEXT_KEPT
    assert "sw pass 1" in notice and "sw pass 2" in notice
    assert notice.count("read 0.1 seconds") == 2
    assert notice.count("write 0.1 seconds") == 2
    assert notice.count("CPU unknown over pass") == 2
    assert "slow 0/1" in notice and "slow 1/2" in notice
    assert notice.count("fast 0/0") == 2
    assert "restart failed" in notice
    assert all(row["watchdog"] == reason for row in ledger_probe.state(store, "sw")["raised_samples"])


@pytest.mark.parametrize("field", ["read", "write"])
def test_refused_probe_requests_are_retained_separately_from_outages(store, field):
    clock = Clock()
    ledger = ProbedLedger(clock, read_s=6.0 if field == "write" else 0.1, write_s=6.0 if field == "read" else 0.1)
    setattr(ledger, f"{field}_error", LedgerRefused("request refused"))
    observe(store, ledger, clock)
    observe(store, ledger, clock)
    rows = ledger_probe.state(store, "sw")["raised_samples"]
    assert all(row[f"{field}_failure"] == "" for row in rows)
    assert all(row[f"{field}_refusal"] == "request refused" for row in rows)
    assert f"{field} 0.1 seconds, request refused" in master_mail(store)[0]
    ledger.read_s, ledger.write_s = 0.1, 0.1
    observe(store, ledger, clock)
    observe(store, ledger, clock)
    assert not ledger_probe.holding(store, "sw")


@pytest.mark.parametrize("departure", ["remove", "stop"])
def test_a_departed_swarm_does_not_hold_an_incident_after_two_answered_sibling_passes(store, departure):
    store.create(SwarmConfig("sibling", "/repo", max_eng=1, max_ci=0))
    clock = Clock()
    ledger = ProbedLedger(clock, read_s=6.0)
    observe(store, ledger, clock)
    observe(store, ledger, clock)
    if departure == "remove":
        for agent in store.agents("sw"):
            store.drop_agent("sw", agent.name)
        store.remove("sw")
    else:
        store.update("sw", state="stopped")
    ledger.read_s = 0.1
    read, write, notify = ledger.metadata, ledger.time_left, ledger.notify
    ledger.metadata = lambda slug: read("sw")
    ledger.time_left = lambda slug, slots, ci_minutes: write("sw", slots, ci_minutes)
    ledger.notify = lambda slug, text: notify("sw", text)
    ledger_probe.observe(store, "sibling", ledger, FakeRuntime(), 3_000, clock=clock, facts=lambda: FACTS)
    assert store.redis.hget(incidents.key("ledger"), "active") == "1"
    ledger_probe.observe(store, "sibling", ledger, FakeRuntime(), 4_000, clock=clock, facts=lambda: FACTS)
    assert store.redis.hget(incidents.key("ledger"), "active") == "0"
    [raised, recovered] = InboxStore(store.redis).pending_items("master@sibling")
    assert "slow" in raised.text
    assert "sibling pass 1" in recovered.text and "sibling pass 2" in recovered.text
    ledger.read_s = 6.0
    for at in (5_000, 6_000):
        ledger_probe.observe(store, "sibling", ledger, FakeRuntime(), at, clock=clock, facts=lambda: FACTS)
    assert store.redis.hget(incidents.key("ledger"), "generation") == "2"


@pytest.mark.parametrize("prior_fast", [0, 1, 41])
def test_a_legacy_incident_names_missing_evidence_and_waits_for_measured_recovery(store, prior_fast):
    import json

    clock = Clock()
    ledger = ProbedLedger(clock)
    store.redis.hset(incidents.key("ledger"), mapping={"active": 1, "generation": 9})
    store.redis.set(
        store.key("sw", "ledger-slow"), json.dumps({"alert": False, "slow": 0, "fast": prior_fast, "notices": []})
    )
    observe(store, ledger, clock)
    assert store.redis.hget(incidents.key("ledger"), "active") == "1"
    assert "triggering passes unknown" in master_mail(store)[0]
    observe(store, ledger, clock)
    assert store.redis.hget(incidents.key("ledger"), "active") == "0"
    assert "sw pass 1" in master_mail(store)[1] and "sw pass 2" in master_mail(store)[1]


def test_missing_legacy_pass_records_restart_the_slow_streak(store):
    import json

    clock = Clock()
    ledger = ProbedLedger(clock, read_s=6.0)
    store.redis.set(store.key("sw", "ledger-slow"), json.dumps({"alert": False, "slow": 1, "fast": 0, "notices": []}))
    assert observe(store, ledger, clock) == []
    assert not ledger_probe.holding(store, "sw") and master_mail(store) == []
    assert observe(store, ledger, clock) == ["raised the ledger slow alert"]
    assert [row["sequence"] for row in ledger_probe.state(store, "sw")["raised_samples"]] == [1, 2]


def test_a_restored_local_alert_does_not_invent_its_missing_triggering_passes(store):
    import json

    clock = Clock()
    ledger = ProbedLedger(clock, read_s=6.0)
    store.redis.set(store.key("sw", "ledger-slow"), json.dumps({"alert": True, "slow": 2}))
    observe(store, ledger, clock)
    assert master_mail(store) == [
        "Ledger incident changed; triggering passes unknown. "
        "Idle and stale claim checks and nudges pause until recovery below four seconds."
    ]
    assert ledger_probe.holding(store, "sw")


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        (
            "raised",
            "The ledger is slow or unavailable; triggering passes unknown. "
            "Idle and stale claim checks and nudges pause until recovery below four seconds.",
        ),
        ("resolved", "The ledger incident is clear; triggering passes unknown."),
    ],
)
def test_a_legacy_push_without_retained_text_names_the_evidence_gap(store, monkeypatch, event, expected):
    from unittest.mock import Mock

    from scripts.swarm import push

    sent = Mock(return_value=True)
    monkeypatch.setattr(push, "send", sent)
    root = incidents.key("ledger")
    store.redis.hset(root, mapping={"active": int(event == "raised"), "generation": 9})
    if event == "resolved":
        store.redis.hset(f"{root}:mail:master@sw", "raised", 9)
    store.redis.rpush(f"{root}:outbox", event)
    clock = Clock()
    observe(store, ProbedLedger(clock), clock)
    assert master_mail(store) == [expected]
    assert sent.call_args.args == ("critical", expected)
    assert store.redis.lrange(f"{root}:outbox", 0, -1) == []


def test_two_failed_passes_raise_the_alert_naming_the_failure(store):
    clock = Clock()
    ledger = ProbedLedger(clock)
    ledger.read_error = TimeoutError("timed out")
    ledger.write_error = SwarmError("ledger sw: ledger server not answering on http://127.0.0.1:8765: timed out")
    ledger.notify_error = OSError("connection refused")
    observe(store, ledger, clock)
    assert observe(store, ledger, clock) == ["raised the ledger slow alert"]
    [mail] = master_mail(store)
    assert "write 0.1 seconds, timed out" in mail and "probe failed" in mail and ledger.notes == []
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


@pytest.mark.parametrize("latency", [4.0, 4.8, 5.0])
def test_recovery_requires_two_passes_below_four_seconds(store, latency):
    clock = Clock()
    ledger = ProbedLedger(clock, write_s=5.7)
    observe(store, ledger, clock)
    observe(store, ledger, clock)
    ledger.write_s = latency
    for _ in range(4):
        assert observe(store, ledger, clock) == []
        assert ledger_probe.holding(store, "sw")
    ledger.write_s = 3.9
    assert observe(store, ledger, clock) == []
    ledger.write_s = latency
    assert observe(store, ledger, clock) == []
    ledger.write_s = 3.9
    assert observe(store, ledger, clock) == []
    assert observe(store, ledger, clock) == ["cleared the ledger slow alert"]
    assert not ledger_probe.holding(store, "sw")


@pytest.mark.parametrize("latency", [0.1, 5.0])
def test_a_failure_only_hold_resumes_on_the_first_answered_fast_pass(store, latency):
    clock = Clock()
    ledger = ProbedLedger(clock, write_s=latency)
    ledger.read_error = TimeoutError("timed out")
    observe(store, ledger, clock)
    observe(store, ledger, clock)
    assert "probe failed" in master_mail(store)[0]
    ledger.read_error, ledger.write_s = None, 0.1
    assert observe(store, ledger, clock) == ["cleared the ledger slow alert"]
    assert not ledger_probe.holding(store, "sw")
    [raised, cleared] = master_mail(store)
    assert cleared == (
        "The ledger server answers fast again: one answered fast pass took "
        "sw pass 3: read 0.1 seconds and write 0.1 seconds, CPU unknown over pass, "
        "streaks before/after slow 2/0, fast 0/1. Idle and stale claim checks resume."
    )


def test_latency_in_a_failed_pass_still_needs_two_recovery_passes(store):
    clock = Clock()
    ledger = ProbedLedger(clock, write_s=5.7)
    ledger.read_error = TimeoutError("timed out")
    observe(store, ledger, clock)
    observe(store, ledger, clock)
    ledger.read_error, ledger.write_s = None, 0.1
    assert observe(store, ledger, clock) == []
    assert ledger_probe.holding(store, "sw")
    assert observe(store, ledger, clock) == ["cleared the ledger slow alert"]


@pytest.mark.parametrize(("latency", "first_fast_clears"), [(5.0, True), (5.7, False)])
def test_a_failed_hold_requires_two_fast_passes_after_a_later_slow_request(store, latency, first_fast_clears):
    clock = Clock()
    ledger = ProbedLedger(clock)
    ledger.read_error = TimeoutError("timed out")
    observe(store, ledger, clock)
    observe(store, ledger, clock)
    ledger.read_error, ledger.write_s = None, latency
    assert observe(store, ledger, clock) == []
    assert ledger_probe.state(store, "sw")["failure_only"] is first_fast_clears
    ledger.write_s = 0.1
    actions = observe(store, ledger, clock)
    if first_fast_clears:
        assert actions == ["cleared the ledger slow alert"]
        assert not ledger_probe.holding(store, "sw")
    else:
        assert actions == [] and ledger_probe.holding(store, "sw")
        assert observe(store, ledger, clock) == ["cleared the ledger slow alert"]


def test_notice_cpu_covers_each_triggering_pass_instead_of_notice_time(store, monkeypatch):
    from scripts.swarm import ledger_host

    clock = Clock()
    ledger = ProbedLedger(clock, read_s=6.0, write_s=2.0)
    readings = iter([(100, 50), (300, 50), (700, 50), (1100, 50)])
    boundaries = []
    monkeypatch.setattr(ledger_host, "server_pid", lambda folder: 51)
    monkeypatch.setattr(ledger_host.os, "sysconf", lambda name: 100)

    def ticks(pid, proc):
        assert pid == 51
        boundaries.append(clock.now)
        return next(readings)

    monkeypatch.setattr(ledger_host, "_stat", ticks)
    observe(store, ledger, clock, 1_000)
    observe(store, ledger, clock, 2_000)
    [notice] = master_mail(store)
    assert "CPU 25 percent over pass" in notice
    assert "CPU 50 percent over pass" in notice
    assert "CPU 182" not in notice
    assert boundaries == [0.0, 8.0, 8.0, 16.0]
    rows = ledger_probe.state(store, "sw")["raised_samples"]
    assert rows[0]["cpu_before"] == {"pid": 51, "start": 50, "ticks": 100, "hertz": 100}
    assert rows[0]["cpu_after"]["ticks"] == 300
    assert rows[1]["cpu_before"]["ticks"] == 700
    assert rows[1]["cpu_after"]["ticks"] == 1100
    assert [row["cpu"] for row in rows] == [25.0, 50.0]
    assert [row["wall_s"] for row in rows] == [8.0, 8.0]
    assert all(row["raise_s"] == 5.0 and row["clear_s"] == 4.0 for row in rows)
    assert all(f"CPU {cpu} percent over pass" in ledger.notes[0] for cpu in (25, 50))


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
    assert len(note) <= ledger_probe.TEXT_KEPT
    for words in (
        "read 12.2 seconds",
        "write 20.5 seconds",
        "timed out",
        "CPU unknown over pass",
        "started 74 minutes ago",
    ):
        assert words in note
    assert "Newest on dev merged 9 minutes ago: Keep a merge wait" in note


def test_unknown_server_facts_are_named_unknown(store):
    clock = Clock()
    ledger = ProbedLedger(clock, read_s=6.0)
    unknown = {"cpu": None, "started_minutes": None, "newest": None, "merged_minutes": None}
    for _ in range(2):
        ledger_probe.observe(store, "sw", ledger, FakeRuntime(), 1_000, clock=clock, facts=lambda: unknown)
    [mail] = master_mail(store)
    assert "CPU unknown over pass" in mail
    assert "Server start time unknown. Newest on dev unknown." in mail


def test_a_cpu_reading_is_shown_in_whole_percent(store, monkeypatch):
    from scripts.swarm import ledger_host

    clock = Clock()
    ledger = ProbedLedger(clock, read_s=6.0)
    monkeypatch.setattr(ledger_host, "snapshot", lambda: {"pid": 1, "start": 1, "ticks": clock.now * 182, "hertz": 100})
    for _ in range(2):
        observe(store, ledger, clock)
    assert "CPU 182 percent over pass" in master_mail(store)[0]
    assert "Server started 74 minutes ago." in master_mail(store)[0]


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
    import json

    clock = Clock()
    ledger = ProbedLedger(clock, read_s=0.2)
    observe(store, ledger, clock)
    held = ledger_probe.state(store, "sw")
    assert (held["slow"], held["fast"], held["notices"]) == (0, 1, [])
    assert json.loads(store.redis.get(store.key("sw", "ledger-slow"))) == held
    ledger.read_s = 6.0
    observe(store, ledger, clock, at=2_000)
    observe(store, ledger, clock, at=3_000)
    held = ledger_probe.state(store, "sw")
    assert (held["slow"], held["fast"], held["alert"], held["raised_at"], held["notices"]) == (2, 0, True, 3_000, [])
    assert [row["at"] for row in held["raised_samples"]] == [2_000, 3_000]
    ledger.read_s = 0.2
    observe(store, ledger, clock)
    observe(store, ledger, clock)
    held = ledger_probe.state(store, "sw")
    assert (held["slow"], held["fast"], held["alert"], held["raised_at"], held["notices"]) == (0, 2, False, 3_000, [])
    assert [row["sequence"] for row in held["cleared_samples"]] == [4, 5]


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
        "The ledger server answers fast again: two swarm passes took "
        "sw pass 3: read 0.2 seconds and write 0.1 seconds, CPU unknown over pass, "
        "streaks before/after slow 2/0, fast 0/1. "
        "sw pass 4: read 0.2 seconds and write 0.1 seconds, CPU unknown over pass, "
        "streaks before/after slow 0/0, fast 1/2. "
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
    from scripts.swarm import stale_nudge
    from scripts.swarm.tick import IDLE_NUDGE_TICKS, _watch_idle

    name = "engineer@a1b2c3-0002"
    stale = 1_001 + stale_nudge.LIMIT_MS
    store.put_agent("sw", AgentRecord(name, "eng", "t1", started_at=1_000))
    ledger, clock = raise_alert(store)
    runtime = FakeRuntime()
    runtime.statuses[name] = "idle"
    for n in range(IDLE_NUDGE_TICKS + 2):
        assert _watch_idle("sw", store, ledger, runtime, {}, worker(store, name), stale + n) == []
    assert InboxStore(store.redis).inbox(name) == [] and worker(store, name).idle_ticks == 0
    ledger.read_s = 0.1
    observe(store, ledger, clock)
    observe(store, ledger, clock)
    actions = _watch_idle("sw", store, ledger, runtime, {}, worker(store, name), stale + 10)
    [item] = InboxStore(store.redis).inbox(name)
    assert actions == [f"nudged stale {name} through its inbox as message {item.id}"] and runtime.nudged == []


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
