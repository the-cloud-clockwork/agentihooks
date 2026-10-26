import re

import pytest

from scripts.doctor import loop
from scripts.inbox.store import InboxStore
from scripts.swarm.health.findings import MINUTE_MS, Finding
from scripts.swarm.store import RedisStore, SwarmConfig, SwarmError
from scripts.swarm_ledger import ledger_comments, ledger_kinds

WATCHED, DOCTOR = "watch", "watch-doctor"
T0 = 1_000 * MINUTE_MS
TASK_ID = re.compile(r"^[A-Za-z0-9][\w.-]{0,63}$")
STALE = Finding(
    "stale claim", "watch-eng-1", "claimed task with no change for 40 minutes", ("task t1",), "30 minutes", 40
)
IDLE = Finding("idle with claim", "watch-eng-2", "idle for 4 ticks on a claimed task", ("task t2",), "3 ticks", 4)


@pytest.fixture
def store():
    import fakeredis

    found = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    found.create(SwarmConfig(DOCTOR, "/repo", 1, 0, state="drained", template="doctor"))
    found.set_peer(DOCTOR, WATCHED)
    return found


def judged(value):
    finding = {**STALE.as_dict(), "id": STALE.id, "measure": STALE.measure}
    return finding, {"value": value, "note": "checked", "by": "master", "at": T0, "measure": STALE.measure}


@pytest.mark.parametrize("value", ["established", "early-real"])
@pytest.mark.parametrize("fix", ["code", "tune"])
def test_a_judged_real_finding_becomes_a_troubleshoot_task_then_a_fix_naming_its_number_and_measure(value, fix):
    cause, fixed = loop.fix_tasks(WATCHED, *judged(value), fix, ())
    measure = f"agentihooks doctor {WATCHED} measure {STALE.id}"
    assert (cause["kind"], fixed["kind"]) == ("troubleshoot", fix)
    assert fixed["depends_on"] == [cause["task"]]
    assert cause["lane"] == fixed["lane"] == "eng" and cause["phase"] == fixed["phase"] == loop.FIX_PHASE
    assert "40" in fixed["contract"]["must"]
    assert measure in fixed["contract"]["check"] and "before and after" in fixed["contract"]["check"]
    assert measure in fixed["description"] and "40" in fixed["description"]
    assert STALE.summary in cause["description"] and "task t1" in cause["description"]
    for task in (cause, fixed):
        assert TASK_ID.match(task["task"])
        assert ledger_comments.problems(task["title"], "item") == []
        ledger_kinds.check(task)


@pytest.mark.parametrize("verdict", [None, "false-positive", "insufficient-evidence", "resolved"])
def test_a_finding_not_judged_real_never_becomes_a_task(verdict):
    finding, judgment = judged(verdict or "established")
    with pytest.raises(SwarmError, match="established or early-real"):
        loop.fix_tasks(WATCHED, finding, judgment if verdict else None, "code", ())


def test_a_fix_task_is_code_or_tune():
    with pytest.raises(SwarmError, match="code or tune"):
        loop.fix_tasks(WATCHED, *judged("established"), "ops", ())


def test_the_same_finding_always_gives_the_same_task_ids():
    first = [t["task"] for t in loop.fix_tasks(WATCHED, *judged("established"), "code", ())]
    again = [t["task"] for t in loop.fix_tasks(WATCHED, *judged("early-real"), "code", ())]
    assert first == again


def pair(fix="code", done=True):
    return [{"id": t["task"], "done": done} for t in loop.fix_tasks(WATCHED, *judged("established"), fix, ())]


@pytest.mark.parametrize("earlier", ["code", "tune"])
def test_a_returned_finding_whose_earlier_pair_is_done_gets_the_next_free_pair_linking_the_earlier(earlier):
    done = pair(earlier)
    cause, fixed = loop.fix_tasks(WATCHED, *judged("established"), "code", done)
    stem = done[0]["id"].removesuffix("-cause")
    assert (cause["task"], fixed["task"]) == (f"{stem}-2-cause", f"{stem}-2-code")
    assert fixed["depends_on"] == [cause["task"]]
    assert all(t["id"] in cause["description"] for t in done)
    for task in (cause, fixed):
        assert TASK_ID.match(task["task"])
        ledger_kinds.check(task)


def test_each_done_pair_moves_the_new_pair_to_the_next_free_ids():
    first = pair()
    second = [{"id": t["task"], "done": True} for t in loop.fix_tasks(WATCHED, *judged("established"), "code", first)]
    cause, fixed = loop.fix_tasks(WATCHED, *judged("established"), "tune", first + second)
    stem = first[0]["id"].removesuffix("-cause")
    assert (cause["task"], fixed["task"]) == (f"{stem}-3-cause", f"{stem}-3-tune")
    assert all(t["id"] in cause["description"] for t in second)
    assert not any(t["id"] in cause["description"] for t in first)


def test_a_finding_with_no_earlier_pair_names_none_in_its_description():
    cause, _ = loop.fix_tasks(WATCHED, *judged("established"), "code", [{"id": "t1", "done": True}])
    assert cause["task"] == pair()[0]["id"]
    assert "earlier" not in cause["description"].lower()


@pytest.mark.parametrize("earlier", ["code", "tune"])
@pytest.mark.parametrize("open_part", [0, 1])
def test_a_finding_whose_pair_is_still_open_is_refused(open_part, earlier):
    tasks = pair(earlier)
    tasks[open_part]["done"] = False
    with pytest.raises(SwarmError, match="still open"):
        loop.fix_tasks(WATCHED, *judged("established"), "code", tasks)


def test_the_pair_texts_name_the_finding_stem_evidence_and_earlier_pair_exactly():
    finding, judgment = judged("established")
    finding = {**finding, "evidence": ["task t1", "task t2"]}
    stem = "fx-44b64fbe"
    cause, _ = loop.fix_tasks(WATCHED, finding, judgment, "code", ())
    text = (
        f"Doctor finding {STALE.id}: {STALE.summary}. Threshold: {STALE.threshold}. "
        "Evidence: task t1; task t2. Show the root cause with evidence, then fix it or file it."
    )
    assert (cause["task"], cause["description"]) == (f"{stem}-cause", text)
    done = [{"id": f"{stem}-cause", "done": True}, {"id": f"{stem}-code", "done": True}]
    cause, _ = loop.fix_tasks(WATCHED, finding, judgment, "tune", done)
    assert cause["description"] == f"{text} Earlier pair {stem}-cause and {stem}-code is done; read its proof first."
    opened = [{**task, "done": False} for task in done]
    with pytest.raises(SwarmError) as refused:
        loop.fix_tasks(WATCHED, finding, judgment, "tune", opened)
    assert str(refused.value) == f"finding {STALE.id} already has tasks {stem}-cause and {stem}-code still open"


def step(store, at, found=(), closed=None, environ=None):
    seen = []

    def collect(watched):
        seen.append(watched)
        return list(found), []

    actions = loop.run(store, DOCTOR, at, collect, lambda: closed.append(at), environ or {})
    return actions, seen


def master_items(store):
    return InboxStore(store.redis).inbox(f"master@{DOCTOR}")


def test_the_doctor_closes_itself_after_two_hours_with_no_new_finding(store):
    closed = []
    step(store, T0, closed=closed)
    step(store, T0 + 119 * MINUTE_MS, closed=closed)
    assert closed == []
    step(store, T0 + 120 * MINUTE_MS, closed=closed)
    assert closed == [T0 + 120 * MINUTE_MS]


def test_a_new_finding_restarts_the_quiet_count_and_reaches_the_doctor_master(store):
    closed = []
    step(store, T0, closed=closed)
    actions, _ = step(store, T0 + 60 * MINUTE_MS, [STALE], closed)
    assert any("new finding" in a for a in actions)
    [item] = master_items(store)
    assert STALE.id in item.text and f"agentihooks doctor {WATCHED} verdict {STALE.id}" in item.text
    step(store, T0 + 150 * MINUTE_MS, [STALE], closed)
    step(store, T0 + 175 * MINUTE_MS, [STALE], closed)
    assert closed == [] and len(master_items(store)) == 1
    step(store, T0 + 185 * MINUTE_MS, [STALE, IDLE], closed)
    assert closed == [] and len(master_items(store)) == 2
    step(store, T0 + 304 * MINUTE_MS, [STALE, IDLE], closed)
    assert closed == []
    step(store, T0 + 305 * MINUTE_MS, [STALE, IDLE], closed)
    assert closed == [T0 + 305 * MINUTE_MS]


def passes(store, ticks):
    ran = []
    for at in ticks:
        _, seen = step(store, at, closed=[])
        ran += [at] * len(seen)
    return ran


def spacing(ran):
    return [later - earlier for earlier, later in zip(ran, ran[1:])]


def run_of_ticks(gaps_s, minutes=180):
    at, ticks = T0, []
    while at < T0 + minutes * MINUTE_MS:
        ticks.append(at)
        at += gaps_s[len(ticks) % len(gaps_s)] * 1000
    return ticks


def test_detection_runs_once_per_interval_on_minute_ticks(store):
    ran = passes(store, [T0 + m * MINUTE_MS for m in range(31)])
    assert ran == [T0, T0 + 10 * MINUTE_MS, T0 + 20 * MINUTE_MS, T0 + 30 * MINUTE_MS]


@pytest.mark.parametrize("gaps_s", [[66], [60, 66, 63], [61, 72], [90]])
def test_passes_are_never_more_than_the_interval_apart_over_a_run_of_ticks(store, gaps_s):
    ran = passes(store, run_of_ticks(gaps_s))
    assert max(spacing(ran)) <= 10 * MINUTE_MS
    assert len(ran) >= 18
    assert min(spacing(ran)) > 10 * MINUTE_MS - 2 * max(gaps_s) * 1000


def test_a_slow_tick_pulls_the_next_pass_forward_only_until_that_pass(store):
    ran = passes(store, [T0, T0 + 4 * MINUTE_MS, T0 + 5 * MINUTE_MS, T0 + 6 * MINUTE_MS + 1])
    assert ran == [T0, T0 + 6 * MINUTE_MS + 1]
    later = T0 + 7 * MINUTE_MS + 1
    ran = passes(store, [later + m * MINUTE_MS for m in range(10)])
    assert ran == [later + 9 * MINUTE_MS]


def test_the_timer_keeps_each_tick_and_the_slowest_gap_since_the_last_pass(store):
    key = loop._timer_key(store, DOCTOR)
    store.redis.hset(key, mapping={"last": T0, "last_new": T0})

    def saved():
        return {k: int(v) for k, v in store.redis.hgetall(key).items()}

    passes(store, [T0 + MINUTE_MS])
    assert saved() == {"last": T0, "last_new": T0, "gap": 0, "tick": T0 + MINUTE_MS}
    passes(store, [T0 + 3 * MINUTE_MS, T0 + 4 * MINUTE_MS])
    assert saved() == {"last": T0, "last_new": T0, "gap": 2 * MINUTE_MS, "tick": T0 + 4 * MINUTE_MS}
    assert passes(store, [T0 + 9 * MINUTE_MS]) == [T0 + 9 * MINUTE_MS]
    assert saved() == {"last": T0 + 9 * MINUTE_MS, "last_new": T0, "gap": 0, "tick": T0 + 9 * MINUTE_MS}


def test_the_interval_and_the_quiet_window_come_from_the_environment(store):
    closed, environ = [], {loop.INTERVAL_ENV: "1", loop.QUIET_ENV: "5"}
    step(store, T0, closed=closed, environ=environ)
    _, seen = step(store, T0 + MINUTE_MS, closed=closed, environ=environ)
    assert seen == [WATCHED] and closed == []
    step(store, T0 + 5 * MINUTE_MS, closed=closed, environ=environ)
    assert closed == [T0 + 5 * MINUTE_MS]


def test_a_stopped_doctor_or_one_without_a_peer_runs_no_pass(store):
    store.update(DOCTOR, state="stopped")
    assert step(store, T0, closed=[]) == ([], [])
    store.update(DOCTOR, state="drained")
    store.clear_peer(DOCTOR)
    assert step(store, T0, closed=[]) == ([], [])


def test_a_restarted_doctor_counts_its_quiet_hours_from_the_restart(store):
    closed = []
    step(store, T0, closed=closed)
    loop.reset(store, DOCTOR)
    step(store, T0 + 200 * MINUTE_MS, closed=closed)
    assert closed == []


def test_a_judged_finding_is_read_back_with_its_detail_and_verdict(store):
    step(store, T0, [STALE], closed=[])
    loop.verdicts(store, DOCTOR).judge(STALE.id, "established", "checked", "master", T0 + MINUTE_MS)
    finding, verdict = loop.judged(store, DOCTOR, STALE.id)
    assert finding["summary"] == STALE.summary and finding["measure"] == 40
    assert verdict["value"] == "established"
    with pytest.raises(SwarmError, match="no Doctor finding"):
        loop.judged(store, DOCTOR, "stale-claim/nobody")


def aged(minutes, *evidence):
    return Finding("inbox past window", "item-1", f"pending for {minutes} minutes", evidence, "window", minutes)


def test_a_judged_finding_whose_only_growth_is_age_stays_judged_until_its_evidence_changes(store):
    step(store, T0, [aged(10, "from a to b", "0 wakes")], closed=[])
    loop.verdicts(store, DOCTOR).judge(aged(10).id, "false-positive", "known", "master", T0 + MINUTE_MS)
    step(store, T0 + 90 * MINUTE_MS, [aged(100, "from a to b", "0 wakes")], closed=[])
    assert len(master_items(store)) == 1
    step(store, T0 + 100 * MINUTE_MS, [aged(110, "from a to b", "1 wake")], closed=[])
    [_, again] = master_items(store)
    assert f"{aged(0).id}: pending for 110 minutes. It came back after a false-positive verdict." in again.text


@pytest.mark.parametrize("value", ["false-positive", "established"])
@pytest.mark.parametrize("minutes", [10, 100])
def test_detector_wording_changes_do_not_return_judged_findings(store, value, minutes):
    before = [
        Finding("inbox past window", f"item-{i}", "pending", ("0 wakes",), "window", 10, evidence_identity=(0, 0))
        for i in range(19)
    ]
    assert len(loop.record(store, DOCTOR, before, T0, MINUTE_MS)) == 19
    for finding in before:
        loop.verdicts(store, DOCTOR).judge(finding.id, value, "known", "master", T0)
    after = [
        Finding(
            "inbox past window",
            f"item-{i}",
            "unread delivery",
            ("recipient has no live occupant", "no wakes"),
            "window",
            minutes,
            evidence_identity=(0, 0),
        )
        for i in range(19)
    ]
    assert loop.record(store, DOCTOR, after, T0 + 90 * MINUTE_MS, MINUTE_MS) == []


def test_new_evidence_identity_returns_a_judged_finding_even_with_unchanged_display(store):
    from scripts.swarm.health.verdicts import VerdictStore

    judgments = VerdictStore(store.redis, loop.verdicts(store, DOCTOR).key)
    first = Finding("inbox past window", "item", "pending", ("same display",), "window", 10, evidence_identity=(0,))
    loop.record(store, DOCTOR, [first], T0, MINUTE_MS)
    judgments.judge(first.id, "established", "known", "master", T0)
    changed = Finding("inbox past window", "item", "pending", ("same display",), "window", 11, evidence_identity=(1,))
    assert loop.record(store, DOCTOR, [changed], T0, MINUTE_MS) == []
    [returned] = loop.record(store, DOCTOR, [changed], T0 + MINUTE_MS, MINUTE_MS)
    assert returned["id"] == first.id
    assert returned["verdict"]["value"] == "established"
    assert loop.record(store, DOCTOR, [changed], T0 + 2 * MINUTE_MS, MINUTE_MS) == []


def test_a_legacy_verdict_adopts_evidence_identity_without_returning_on_new_wording(store):
    first = Finding("inbox past window", "item", "pending", ("old wording",), "window", 10)
    loop.record(store, DOCTOR, [first], T0, MINUTE_MS)
    loop.verdicts(store, DOCTOR).judge(first.id, "false-positive", "known", "master", T0)
    changed = Finding("inbox past window", "item", "pending", ("new wording",), "window", 100, evidence_identity=(0,))
    assert loop.record(store, DOCTOR, [changed], T0 + MINUTE_MS, MINUTE_MS) == []
    _, verdict = loop.judged(store, DOCTOR, first.id)
    assert verdict["evidence_identity"] == [0]
    assert verdict["measure"] == 10
    assert verdict["at"] == T0
    changed = Finding("inbox past window", "item", "pending", ("new wording",), "window", 101, evidence_identity=(1,))
    assert len(loop.record(store, DOCTOR, [changed], T0 + 2 * MINUTE_MS, MINUTE_MS)) == 1


def test_inbox_wording_does_not_return_a_finding_but_a_new_wake_does(store, monkeypatch):
    from scripts.doctor import inbox

    item = {
        "id": "item",
        "sender": "master",
        "address": "operator",
        "text": "work",
        "state": "pending",
        "created_at": T0 - 10 * MINUTE_MS,
        "history": [],
    }
    monkeypatch.setattr(inbox, "_recipient", lambda *args: "old recipient wording")
    first = inbox.past_window([item], T0, MINUTE_MS)
    assert len(loop.record(store, DOCTOR, first, T0, MINUTE_MS)) == 1
    loop.verdicts(store, DOCTOR).judge(first[0].id, "false-positive", "known", "master", T0)
    monkeypatch.setattr(inbox, "_recipient", lambda *args: "recipient has no live occupant")
    after = inbox.past_window([item], T0 + 90 * MINUTE_MS, MINUTE_MS)
    assert after[0].evidence != first[0].evidence
    assert loop.record(store, DOCTOR, after, T0 + 90 * MINUTE_MS, MINUTE_MS) == []
    item["history"].append({"event": "woken", "at": T0 + 90 * MINUTE_MS})
    wake = inbox.past_window([item], T0 + 91 * MINUTE_MS, MINUTE_MS)
    assert len(loop.record(store, DOCTOR, wake, T0 + 91 * MINUTE_MS, MINUTE_MS)) == 1
