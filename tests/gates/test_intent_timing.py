import time

from scripts.gates import intent
from scripts.gates.verdicts import Verdicts
from tests.gates.test_intent import DOC, NOW, PR, SLUG, TASK, Ledger, Mail

DELAY_S = 0.05


def test_a_delayed_verdict_records_its_persistence_time_apart_from_the_pass_start(tmp_path):
    def slow(state):
        time.sleep(DELAY_S)
        return "pass", "the phase can use it"

    started = int(time.time() * 1000)
    actions = intent.Check(SLUG, "enforce", NOW, Ledger(), Mail(), lambda url: PR, slow, home=tmp_path).run(DOC)
    record = Verdicts(SLUG, "intent", tmp_path).read(TASK)
    assert actions == [f"task {TASK} intent check pass"]
    assert record["at"] == NOW
    assert record["persisted_at"] - started >= DELAY_S * 1000
    assert record["persisted_at"] <= int(time.time() * 1000)


def test_a_kept_verdict_keeps_its_persistence_time(tmp_path):
    state = Verdicts(SLUG, "intent", tmp_path)
    check = intent.Check(SLUG, "coach", NOW + 60_000, Ledger(), Mail(), lambda url: PR, None, home=tmp_path)
    previous = {
        "verdict": "pass",
        "reason": "the phase can use it",
        "at": NOW,
        "coach_rounds": 0,
        "head": "h1",
        "persisted_at": NOW + 5_000,
    }
    check._keep(DOC["tasks"][0], previous, state)
    record = state.read(TASK)
    assert record["at"] == NOW
    assert record["persisted_at"] == NOW + 5_000
