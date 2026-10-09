import copy
import threading
from contextvars import ContextVar

from scripts.gates import intent, intent_history
from scripts.gates.verdicts import Verdicts
from tests.gates.test_intent import DOC, PR, Ledger, Mail


def test_judgments_overlap_with_two_workers_and_effects_keep_the_caller_order(tmp_path, monkeypatch):
    doc = copy.deepcopy(DOC)
    doc["tasks"] = [
        {
            **DOC["tasks"][0],
            "id": f"t{i}",
            "title": f"task{i}",
            "state": "claimed",
            "pr_url": f"https://github.com/o/r/pull/{i}",
        }
        for i in range(4)
    ]
    caller = threading.get_ident()
    context = ContextVar("intent_pool_caller", default="missing")
    context.set("caller")
    barrier, lock = threading.Barrier(2), threading.Lock()
    active = peak = 0
    workers, history, effects = [], [], []
    ledger = Ledger()
    write = Verdicts.write
    append = intent_history.append
    update = ledger.update_task

    def judged(state):
        nonlocal active, peak
        assert context.get() == "caller"
        assert threading.get_ident() != caller
        with lock:
            active += 1
            peak = max(peak, active)
            workers.append(threading.get_ident())
        barrier.wait(timeout=1)
        with lock:
            active -= 1
        return "pass", "ok"

    def recorded(self, subject, verdict, *args, **kwargs):
        assert threading.get_ident() == caller
        effects.append((subject, verdict))
        return write(self, subject, verdict, *args, **kwargs)

    def appended(slug, row, home):
        assert threading.get_ident() == caller
        history.append(row["task"])
        return append(slug, row, home)

    def updated(slug, task, fields, by="swarm"):
        assert threading.get_ident() == caller
        return update(slug, task, fields, by)

    monkeypatch.setattr(Verdicts, "write", recorded)
    monkeypatch.setattr(intent_history, "append", appended)
    monkeypatch.setattr(ledger, "update_task", updated)
    check = intent.Check("proof", "coach", 123, ledger, Mail(), lambda url: {**PR, "head": url}, judged, home=tmp_path)
    actions = check.run(doc)
    assert peak == 2
    assert len(set(workers)) == 2
    assert history == ["t0", "t1", "t2", "t3"]
    assert [row[1] for row in ledger.updates] == history
    assert actions == [f"task t{i} intent check pass" for i in range(4)]
    assert [subject for subject, verdict in effects if verdict == "pass"] == [
        task for task in history for _ in range(2)
    ]


def test_completed_judgments_apply_in_task_order_even_when_the_later_one_finishes_first(tmp_path, monkeypatch):
    doc = copy.deepcopy(DOC)
    doc["tasks"] = [
        {**DOC["tasks"][0], "id": f"t{i}", "title": f"task{i}", "pr_url": f"https://github.com/o/r/pull/{i}"}
        for i in range(2)
    ]
    completed = threading.Event()
    finished, applied = [], []

    def judged(state):
        task = state["task"]
        if task == "task0":
            assert completed.wait(timeout=1)
        finished.append(task)
        if task == "task1":
            completed.set()
        return "pass", task

    monkeypatch.setattr(intent_history, "append", lambda slug, row, home: applied.append(row["task"]))
    check = intent.Check("proof", "observe", 123, Ledger(), Mail(), lambda url: PR, judged, home=tmp_path)
    assert check.run(doc) == ["task t0 intent check pass", "task t1 intent check pass"]
    assert finished == ["task1", "task0"]
    assert applied == ["t0", "t1"]


def test_the_pool_bound_is_two_workers(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    bounds = []

    def pool(*, max_workers):
        bounds.append(max_workers)
        return ThreadPoolExecutor(max_workers=max_workers)

    monkeypatch.setattr(intent, "ThreadPoolExecutor", pool)
    check = intent.Check(
        "proof", "observe", 123, Ledger(), Mail(), lambda url: PR, lambda state: ("pass", "ok"), home=tmp_path
    )
    check.run(DOC)
    assert bounds == [2]


def test_an_unchanged_later_task_restores_its_verdict_after_the_earlier_judgment(tmp_path, monkeypatch):
    doc = copy.deepcopy(DOC)
    doc["tasks"] = [{**DOC["tasks"][0], "id": f"t{i}", "pr_url": f"https://github.com/o/r/pull/{i}"} for i in range(2)]
    Verdicts("proof", "intent-coach", tmp_path).write(
        "t1", "pass", "kept", 1, coach_rounds=0, head="same", url=doc["tasks"][1]["pr_url"]
    )
    writes, read = [], []
    write = Verdicts.write

    def recorded(self, subject, verdict, *args, **kwargs):
        if self.gate == "intent":
            writes.append(subject)
        return write(self, subject, verdict, *args, **kwargs)

    monkeypatch.setattr(Verdicts, "write", recorded)
    check = intent.Check(
        "proof",
        "coach",
        123,
        Ledger(),
        Mail(),
        lambda url: read.append(url) or {**PR, "head": "new"},
        lambda state: ("pass", "ok"),
        home=tmp_path,
        head=lambda url: "same" if url.endswith("/1") else "new",
    )
    check.run(doc)
    assert writes == ["t0", "t0", "t1", "t1"]
    assert read == [doc["tasks"][0]["pr_url"]]
