import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime


@pytest.fixture
def held():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=0, max_ci=0, state="paused"))
    ledger = FakeLedger([{"id": "t1", "title": "Hive telemetry", "awaiting": "verify", "contract": {"after_at": ""}}])
    ledger.stamps = {"tasks/t1/awaiting": {"at": 1}}
    return store, ledger, FakeRuntime()


def notices(store):
    return [item for item in InboxStore(store.redis).inbox("master@sw") if "Hive telemetry" in item.text]


@pytest.mark.feature("swarm-claim-hold")
def test_tick_tells_the_master_once_when_a_verification_hold_has_no_window_for_over_an_hour(held):
    store, ledger, runtime = held
    tick("sw", store, ledger, runtime, 3_600_001)
    assert notices(store) == []
    actions = tick("sw", store, ledger, runtime, 3_600_002)
    assert "told the master about verification hold t1" in actions
    tick("sw", store, ledger, runtime, 3_600_003)
    items = notices(store)
    assert len(items) == 1
    assert items[0].sender == "swarm"
    assert items[0].text == (
        "Task t1 (Hive telemetry) awaits verification without a proof window "
        "for more than an hour. Set contract.after_at to its proof time, or reopen it with "
        "agentihooks api --slug sw --as master task set t1 awaiting="
    )
    tick("sw", store, ledger, runtime, 90_000_001)
    assert len(notices(store)) == 1
    tick("sw", store, ledger, runtime, 90_000_002)
    assert len(notices(store)) == 2


def test_tick_does_not_raise_a_verification_hold_with_a_future_window(held):
    store, ledger, runtime = held
    ledger.rows["t1"]["contract"]["after_at"] = 9_000_000
    tick("sw", store, ledger, runtime, 3_600_002)
    assert notices(store) == []


def test_clearing_a_scheduled_window_starts_a_new_hour_without_a_window(held):
    store, ledger, runtime = held
    tick("sw", store, ledger, runtime, 2)
    ledger.rows["t1"]["contract"]["after_at"] = 20_000_000
    tick("sw", store, ledger, runtime, 4_000_000)
    ledger.rows["t1"]["contract"]["after_at"] = ""
    ledger.stamps["tasks/t1/contract"] = {"at": 5_000_000}
    tick("sw", store, ledger, runtime, 5_000_001)
    assert notices(store) == []
    tick("sw", store, ledger, runtime, 8_600_000)
    assert notices(store) == []
    tick("sw", store, ledger, runtime, 8_600_001)
    assert len(notices(store)) == 1


@pytest.mark.parametrize(
    "fields", [{"state": "done"}, {"state": "blocked"}, {"awaiting": "fix"}, {"out_of_scope": True}]
)
def test_only_open_verification_holds_need_a_master_notice(held, fields):
    from scripts.swarm import claim_hold

    store, ledger, _ = held
    ledger.rows["t1"].update(fields)
    claim_hold.verification_notices("sw", store, ledger.state("sw"), 3_600_002)
    assert notices(store) == []


def test_an_unstamped_hold_is_dated_from_first_observation(held):
    from scripts.swarm import claim_hold

    store, ledger, _ = held
    ledger.stamps = {}
    for at in (1, 3_600_001):
        claim_hold.verification_notices("sw", store, ledger.state("sw"), at)
        assert notices(store) == []
    claim_hold.verification_notices("sw", store, ledger.state("sw"), 3_600_002)
    assert len(notices(store)) == 1


@pytest.mark.parametrize("metadata", [{}, {"_meta": {}}])
def test_a_document_without_stamps_times_the_hold_from_observation(held, metadata):
    from scripts.swarm import claim_hold

    store, ledger, _ = held
    doc = {"tasks": ledger.tasks("sw"), **metadata}
    for at in (100, 3_600_100):
        assert claim_hold.verification_notices("sw", store, doc, at) == []
        assert notices(store) == []
    assert claim_hold.verification_notices("sw", store, doc, 3_600_101) == [
        "told the master about verification hold t1"
    ]
    assert len(notices(store)) == 1


@pytest.mark.parametrize(
    "earlier",
    [
        {"state": "done"},
        {"contract": {"after_at": 20_000_000}},
        {"recent": True},
        {"notified": True},
    ],
)
@pytest.mark.feature("swarm-claim-hold")
def test_a_skipped_hold_does_not_hide_the_eligible_task_behind_it(held, earlier):
    from scripts.swarm import claim_hold

    store, ledger, _ = held
    doc = ledger.state("sw")
    other = {**doc["tasks"][0], "id": "t0", **earlier}
    doc["tasks"].insert(0, other)
    ledger.stamps["tasks/t0/awaiting"] = {"at": 9_999_900 if earlier.get("recent") else 1}
    if earlier.get("notified"):
        claim_hold.verification_notices("sw", store, {**doc, "tasks": [other]}, 9_999_999)
    assert claim_hold.verification_notices("sw", store, doc, 10_000_000) == [
        "told the master about verification hold t1"
    ]
    assert len(notices(store)) == (2 if earlier.get("notified") else 1)


def test_each_task_keeps_its_own_observed_hold_and_daily_notice(held):
    from scripts.swarm import claim_hold

    store, ledger, _ = held
    ledger.stamps = {}
    claim_hold.verification_notices("sw", store, ledger.state("sw"), 100)
    ledger.rows["t2"] = {**ledger.rows["t1"], "id": "t2"}
    claim_hold.verification_notices("sw", store, ledger.state("sw"), 200)
    claim_hold.verification_notices("sw", store, ledger.state("sw"), 3_600_101)
    assert [item.text.split()[1] for item in notices(store)] == ["t1"]
    claim_hold.verification_notices("sw", store, ledger.state("sw"), 3_600_201)
    assert {item.text.split()[1] for item in notices(store)} == {"t1", "t2"}


def test_the_same_task_in_two_swarms_has_independent_hold_and_notice_times(held):
    from scripts.swarm import claim_hold

    store, ledger, _ = held
    ledger.stamps = {}
    doc = ledger.state("sw")
    claim_hold.verification_notices("sw", store, doc, 100)
    claim_hold.verification_notices("other", store, doc, 200)
    claim_hold.verification_notices("sw", store, doc, 3_600_101)
    claim_hold.verification_notices("other", store, doc, 3_600_101)
    assert InboxStore(store.redis).inbox("master@other") == []
    claim_hold.verification_notices("other", store, doc, 3_600_201)
    items = InboxStore(store.redis).inbox("master@other")
    assert len(items) == 1
    assert "task set t1 awaiting=" in items[0].text


LAB_NOW = 1_800_000_000_000
BEHIND = "the lab is {} minutes behind on pull request grading"


def lab(wait_s, at=None, name="pull_request"):
    """A stored lab block whose pull request class predicts ``wait_s`` seconds, read at ``at`` or now."""
    import time

    entry = {"pending": 4, "running": 2, "predicted_wait_s": wait_s}
    read = int(time.time() * 1000) if at is None else at
    capacity = {"classes": {name: entry}, "slots": {"total": 2, "busy": 2, "free": 0}}
    return {"url": "http://lab", "at": read, "capacity": capacity, "error": "", "error_at": 0}


@pytest.mark.feature("swarm-lab-behind-hold")
def test_the_lab_holds_eng_claims_outside_the_fix_phase_while_pull_request_grading_runs_behind():
    import time

    from scripts.swarm import claim_hold

    eng = {"id": "t1", "lane": "eng", "phase": "p1"}
    assert claim_hold.ci_behind(eng, lab(35 * 60), 20, "fix") == BEHIND.format(35)
    assert claim_hold.ci_behind(eng, lab(21 * 60), 20, "fix") == BEHIND.format(21)
    assert claim_hold.ci_behind(eng, lab(21 * 60 - 1), 20, "fix") == ""
    assert claim_hold.ci_behind({**eng, "lane": "ci"}, lab(35 * 60), 20, "fix") == ""
    assert claim_hold.ci_behind({**eng, "phase": "fix"}, lab(35 * 60), 20, "fix") == ""
    assert claim_hold.ci_behind({"id": "t2", "phase": ""}, lab(35 * 60), 20, "") == BEHIND.format(35)
    assert claim_hold.ci_behind(eng, lab(35 * 60), 40, "fix") == ""
    assert claim_hold.ci_behind(eng, lab(35 * 60, int(time.time() * 1000) - 600_001), 20, "fix") == ""


@pytest.mark.feature("swarm-lab-behind-hold")
def test_a_stale_failed_missing_or_unpredicted_lab_reading_never_holds():
    from scripts.swarm import claim_hold

    assert claim_hold.lab_minutes(lab(35 * 60, LAB_NOW - 600_000), LAB_NOW) == 35
    assert claim_hold.lab_minutes(lab(35 * 60, LAB_NOW - 600_001), LAB_NOW) == 0
    assert claim_hold.lab_minutes(None, LAB_NOW) == 0
    assert claim_hold.lab_minutes({"url": "http://lab", "error": "refused", "error_at": LAB_NOW}, LAB_NOW) == 0
    assert claim_hold.lab_minutes(lab(None, LAB_NOW), LAB_NOW) == 0
    assert claim_hold.lab_minutes(lab(35 * 60, LAB_NOW, "queue"), LAB_NOW) == 0
    unread = {"capacity": lab(35 * 60)["capacity"]}
    assert claim_hold.lab_minutes(unread, claim_hold.LAB_FRESH_MS + 1) == 0
    assert [claim_hold.wait_minutes(wait) for wait in (None, -1.0, 0.0, 59.9, 1199.0)] == [0, 0, 0, 0, 19]


@pytest.mark.feature("swarm-lab-behind-hold")
def test_the_lab_threshold_reads_its_environment_variable():
    from scripts.swarm.health.findings import ENV, ci_behind_minutes

    assert ENV["ci_behind_minutes"] == "AGENTIHOOKS_HEALTH_CI_BEHIND_MINUTES"
    assert ci_behind_minutes({}) == 20
    assert ci_behind_minutes({"AGENTIHOOKS_HEALTH_CI_BEHIND_MINUTES": "45"}) == 45
    assert ci_behind_minutes({"AGENTIHOOKS_HEALTH_CI_BEHIND_MINUTES": "0"}) == 20
    assert ci_behind_minutes({"AGENTIHOOKS_HEALTH_CI_BEHIND_MINUTES": "1"}) == 1


@pytest.fixture
def running():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1))
    return store


@pytest.mark.feature("swarm-lab-behind-hold")
def test_the_tick_claims_no_eng_task_while_the_lab_is_behind_and_resumes_below_the_threshold(running):
    import json

    from scripts.swarm import tick as tick_module

    store = running
    rows = FakeLedger([{"id": "t1", "lane": "eng"}, {"id": "t2", "lane": "ci"}, {"id": "t3", "lane": "eng"}])
    doc = rows.state("sw")

    def ready(lane):
        return [task["id"] for task in tick_module.ready_in_order("sw", store, rows.rows, doc, lane)]

    store.redis.set(store.key("sw", "lab-capacity"), json.dumps(lab(35 * 60)))
    assert (ready("eng"), ready("ci")) == ([], ["t2"])
    store.redis.set(store.key("sw", "lab-capacity"), json.dumps(lab(5 * 60)))
    assert (ready("eng"), ready("ci")) == (["t1", "t3"], ["t2"])


@pytest.mark.feature("swarm-lab-behind-hold")
def test_a_full_lab_pool_the_swarm_reads_holds_eng_claims_and_the_same_queue_on_an_idle_pool_does_not(
    running: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    import time

    from scripts.ci_lab import metrics as lab_metrics
    from scripts.ci_lab.capacity import Load
    from scripts.swarm import claim_hold, lab_capacity
    from scripts.swarm import tick as tick_module

    monkeypatch.delenv("AGENTIHOOKS_HEALTH_CI_BEHIND_MINUTES", raising=False)
    queued = {"pull_request": (900.0, 600.0)}
    full = Load(queued, {"pull_request": 2}, slots_total=2, slots_busy=2, frees_in=(900.0, 600.0))
    served: list[Load] = [full]
    server = lab_metrics.serve("127.0.0.1", 0, lambda: served[0], lab_metrics.Registry())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    rows = FakeLedger([{"id": "t1", "lane": "eng"}, {"id": "t2", "lane": "ci"}])
    eng = {"id": "t1", "lane": "eng", "phase": ""}

    def ready(lane: str) -> list[str]:
        return [task["id"] for task in tick_module.ready_in_order("sw", running, rows.rows, rows.state("sw"), lane)]

    try:
        lab_capacity.capture(running, "sw", url, int(time.time() * 1000))
        reading = lab_capacity.status(running, "sw")
        hold = claim_hold.lab_reason(running, "sw", "fix")
        named = (hold(eng), hold({**eng, "lane": "ci"}), hold({**eng, "phase": "fix"}))
        held = (ready("eng"), ready("ci"))
        served[0] = Load(queued, {}, slots_total=2, slots_busy=0)
        lab_capacity.capture(running, "sw", url, int(time.time() * 1000))
        resumed = (claim_hold.lab_reason(running, "sw", "fix")(eng), ready("eng"), ready("ci"))
    finally:
        server.shutdown()
        server.server_close()
    assert reading is not None
    assert reading["capacity"]["slots"] == {"total": 2, "busy": 2, "free": 0}
    assert reading["capacity"]["classes"]["pull_request"]["predicted_wait_s"] == 1500.0
    assert named == (BEHIND.format(25), "", "")
    assert held == ([], ["t2"])
    assert resumed == ("", ["t1"], ["t2"])
