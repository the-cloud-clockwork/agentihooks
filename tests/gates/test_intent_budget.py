"""The intent check stays inside its tick step budget with thirty tasks in pr."""

import json
import re
import threading
from concurrent import futures
from dataclasses import replace
from typing import Any, cast

import pytest

from scripts.gates import intent
from scripts.gates.verdicts import Verdicts
from tests.gates.test_intent import DOC, NOW, SLUG, Ledger, Mail

COUNT = 30
TITLE, BODY = "Add the intent gate", "Closes 1"


def _doc() -> dict:
    tasks = [
        {
            "id": f"t{i}",
            "phase": "p8",
            "title": f"Intent check {i}",
            "description": "Refuse merge on a failed check.",
            "state": "pr",
            "pr_url": f"https://github.com/o/r/pull/{i}",
            "claimed_by": f"engineer@1-{i}",
        }
        for i in range(COUNT)
    ]
    return {**DOC, "tasks": tasks}


def _pulls(doc: dict, draft: bool = False, home: object = None) -> tuple[intent.Pulls, list[list[str]]]:
    reads: list[list[str]] = []
    found = {
        task["pr_url"]: {"head": f"h{task['id']}", "title": TITLE, "body": BODY, "state": "OPEN", "draft": draft}
        for task in doc["tasks"]
    }

    def read(urls: list[str]) -> dict[str, dict]:
        reads.append(list(urls))
        return {url: found[url] for url in urls if url in found}

    return intent.Pulls(read, intent.watched_urls(SLUG, doc, "coach", home)), reads


def _check(mode: str, pulls: intent.Pulls, calls: list[str], tmp_path, now: int = NOW) -> intent.Check:
    def view(url: str) -> dict:
        calls.append(f"view {url}")
        return {"title": TITLE, "body": BODY, "files": [], "head": "ht" + url.rsplit("/", 1)[1]}

    def ask(state: dict) -> tuple[str, str]:
        calls.append("ask")
        return "pass", "the phase can use it"

    def named(url: str) -> dict:
        calls.append(f"named {url}")
        return {"url": url, "merged": False}

    return intent.Check(
        SLUG,
        mode,
        now,
        Ledger(),
        Mail(),
        view,
        ask,
        home=tmp_path,
        head=pulls.head,
        named=named,
        text=pulls.text,
        merged=pulls.merged,
        draft=pulls.draft,
        limit=intent.JUDGED_PER_PASS,
    )


@pytest.mark.feature("swarm-intent-check-budget")
def test_thirty_unchanged_coached_pulls_make_one_batched_read_and_no_judgment(tmp_path):
    doc = _doc()
    coaching = Verdicts(SLUG, "intent-coach", tmp_path)
    for task in doc["tasks"]:
        coaching.write(
            task["id"],
            "pass",
            "the phase can use it",
            NOW - 60_000,
            coach_rounds=0,
            head=f"h{task['id']}",
            phase="p8",
            inputs=intent._fingerprint(doc, task),
            text=intent.text_key(TITLE, BODY),
            named_pending=[f"https://github.com/o/other/pull/{task['id']}"],
            url=task["pr_url"],
        )
    pulls, reads = _pulls(doc, home=tmp_path)
    calls: list[str] = []
    actions = _check("coach", pulls, calls, tmp_path).run(doc)
    assert actions == []
    assert calls == []
    assert len(reads) == 1
    assert len(reads[0]) == 2 * COUNT


@pytest.mark.feature("swarm-intent-check-budget")
@pytest.mark.parametrize("mode", ["coach", "enforce"])
def test_thirty_changed_pulls_judge_at_most_the_pass_limit_and_the_rest_later(tmp_path, mode):
    doc = _doc()
    pulls, _ = _pulls(doc)
    first: list[str] = []
    _check(mode, pulls, first, tmp_path).run(doc)
    second: list[str] = []
    _check(mode, pulls, second, tmp_path, NOW + 60_000).run(doc)
    judged = [task["id"] for task in doc["tasks"] if Verdicts(SLUG, "intent", tmp_path).read(task["id"])]
    assert first.count("ask") == intent.JUDGED_PER_PASS
    assert second.count("ask") == intent.JUDGED_PER_PASS
    assert len([c for c in first if c.startswith("view")]) == intent.JUDGED_PER_PASS
    assert len(judged) == 2 * intent.JUDGED_PER_PASS


@pytest.mark.feature("swarm-intent-check-budget")
def test_a_changed_pull_is_judged_off_the_pass_and_settled_by_a_later_pass(tmp_path):
    doc = _doc()
    pulls, _ = _pulls(doc)
    released, asked = threading.Event(), []

    def ask(state: dict) -> tuple[str, str]:
        asked.append(state)
        released.wait(5)
        return "pass", "the phase can use it"

    judging = intent.Judging()
    check = replace(_check("coach", pulls, [], tmp_path), ask=ask, judging=judging)
    first = check.run(doc)
    in_pass = [task["id"] for task in doc["tasks"] if Verdicts(SLUG, "intent", tmp_path).read(task["id"])]
    released.set()
    futures.wait([future for _, future in judging.running.values()], timeout=5)
    second = check.run(doc)
    settled = [task["id"] for task in doc["tasks"] if Verdicts(SLUG, "intent", tmp_path).read(task["id"])]
    assert first == [] and in_pass == []
    assert len(settled) == intent.JUDGED_PER_PASS
    assert second == [f"task {task} intent check pass" for task in settled]


@pytest.mark.feature("swarm-intent-check-budget")
def test_only_a_process_that_keeps_running_passes_judges_off_the_pass(monkeypatch):
    from scripts.swarm import controller

    stops = []

    def stop(*args: object) -> None:
        stops.append(intent.TICK_JUDGING)
        raise KeyboardInterrupt

    nothing: Any = None
    monkeypatch.setattr(controller, "_pass", lambda *args: None)
    assert controller._passes(nothing, None, nothing, True, controller.redis_grace.Outage(1.0)) == 0
    assert intent.TICK_JUDGING is None
    monkeypatch.setattr(controller, "_cadence_wait", stop)
    with pytest.raises(KeyboardInterrupt):
        controller._passes(nothing, None, nothing, False, controller.redis_grace.Outage(1.0))
    kept = intent.TICK_JUDGING
    intent.keep_judging()
    assert isinstance(kept, intent.Judging) and stops == [kept] and intent.TICK_JUDGING is kept


class _Graph:
    """A source that answers each batched pull request text read from ``pulls`` and records each document."""

    def __init__(self, pulls: dict[str, dict[str, Any]]) -> None:
        """Answer reads of ``pulls``."""
        self.pulls, self.documents = pulls, []

    def graph(self, document: str, variables: dict[str, str], *, deadline: Any) -> dict[str, Any]:
        """Answer each alias of ``document`` with its pull, or None when unknown."""
        self.documents.append(document)
        aliases = re.findall(r'(p\d+):resource\(url:"([^"]+)"\)', document)
        return {"data": {alias: self.pulls.get(url) for alias, url in aliases}}


@pytest.mark.feature("swarm-intent-check-budget")
def test_pull_texts_reads_twenty_pulls_a_document_and_keeps_only_those_with_a_head():
    from scripts.swarm import ledger_events

    urls = [f"https://github.com/o/r/pull/{i}" for i in range(25)]
    raw = {
        url: {"headRefOid": f"h{i}", "title": f"T{i}", "body": "B", "state": "OPEN", "isDraft": i == 3}
        for i, url in enumerate(urls)
    }
    raw[urls[1]] = {"headRefOid": "h1", "title": None, "body": None}
    raw[urls[2]] = {"title": "no head"}
    source = _Graph(raw)
    found = ledger_events.pull_texts([*urls, urls[0]], cast(Any, source))
    expected = [
        "{"
        + " ".join(f"p{i}:" + ledger_events.PULL_TEXT.replace("$url", json.dumps(url)) for i, url in enumerate(batch))
        + "}"
        for batch in (urls[:20], urls[20:])
    ]
    assert source.documents == expected
    assert sorted(found) == sorted(url for url in urls if url != urls[2])
    assert found[urls[0]] == {"head": "h0", "title": "T0", "body": "B", "state": "OPEN", "draft": False}
    assert found[urls[1]] == {"head": "h1", "title": "", "body": "", "state": "", "draft": False}
    assert found[urls[3]]["draft"] is True
    assert ledger_events.pull_texts([], cast(Any, source)) == {} and len(source.documents) == 2


@pytest.mark.feature("swarm-intent-check-budget")
def test_pulls_reads_an_unbatched_url_alone_once():
    reads: list[list[str]] = []
    pull = {"head": "h", "title": "T", "body": "", "state": "MERGED", "draft": False}

    def read(urls: list[str]) -> dict[str, dict[str, Any]]:
        reads.append(urls)
        return {url: pull for url in urls if url != "unread"}

    pulls = intent.Pulls(read, ["a"])
    assert (pulls.merged("b"), pulls.head("b"), pulls.head("unread"), pulls.head("unread")) == (True, "h", None, None)
    assert reads == [["a"], ["b"], ["unread"]]


@pytest.mark.feature("swarm-intent-check-budget")
def test_a_judgment_is_held_at_its_key_and_a_new_key_cancels_a_waiting_one():
    judging = intent.Judging(workers=1)
    gate = threading.Event()
    subject, other = ("sw", "t1"), ("sw", "t2")
    assert judging.take(("sw", "blocker"), "k", lambda: gate.wait(5)) is None
    assert judging.take(subject, "old", lambda: "old") is None
    waiting = judging.running[subject][1]
    assert judging.holds(subject, "old") and not judging.holds(subject, "new") and not judging.holds(other, "old")
    assert judging.take(subject, "new", lambda: "new") is None
    assert waiting.cancelled() and judging.holds(subject, "new")
    gate.set()
    futures.wait([judging.running[subject][1]], timeout=5)
    taken = judging.take(subject, "new", lambda: "again")
    assert taken is not None and taken.result() == "new" and not judging.holds(subject, "new")
    judging.keep("sw", {"t1"})
    assert ("sw", "blocker") not in judging.running


@pytest.mark.feature("swarm-intent-check-budget")
def test_a_judgment_key_is_the_task_inputs_and_its_pull_head_and_text(tmp_path):
    doc = _doc()
    pulls, _ = _pulls(doc)
    task = doc["tasks"][4]
    key = _check("coach", pulls, [], tmp_path)._key(doc, task)
    assert key == (intent._fingerprint(doc, task), "ht4", intent.text_key(TITLE, BODY))


@pytest.mark.feature("swarm-intent-check-budget")
def test_off_pass_judging_runs_on_two_workers_and_a_held_judgment_leaves_room_for_another(tmp_path):
    assert intent.Judging().pool._max_workers == 2
    doc = _doc()
    pulls, _ = _pulls(doc)
    released = threading.Event()
    judging = intent.Judging()

    def ask(state: dict[str, Any]) -> tuple[str, str]:
        released.wait(5)
        return "pass", "the phase can use it"

    check = replace(_check("coach", pulls, [], tmp_path), ask=ask, judging=judging, limit=1)
    check.run(doc)
    first = set(judging.running)
    check.run(doc)
    released.set()
    assert len(first) == 1 and first < set(judging.running) and len(judging.running) == 2


@pytest.mark.feature("swarm-intent-check-budget")
def test_a_judgment_made_at_an_old_head_is_never_settled(tmp_path):
    doc = _doc()
    pulls, _ = _pulls(doc)
    judging = intent.Judging()
    check = replace(_check("coach", pulls, [], tmp_path), judging=judging, limit=1)
    check.run(doc)
    [(subject, (_, started))] = judging.running.items()
    futures.wait([started], timeout=5)
    url = next(task["pr_url"] for task in doc["tasks"] if task["id"] == subject[1])
    pulls.found[url] = {**pulls.found[url], "head": "pushed"}
    assert check.run(doc) == []
    assert Verdicts(SLUG, "intent", tmp_path).read(subject[1]) is None
    assert cast(tuple[Any, ...], judging.running[subject][0])[1] == "pushed"


@pytest.mark.feature("swarm-intent-check-budget")
def test_thirty_draft_pulls_are_recorded_draft_without_a_pull_request_view(tmp_path):
    doc = _doc()
    pulls, _ = _pulls(doc, draft=True)
    calls: list[str] = []
    _check("coach", pulls, calls, tmp_path).run(doc)
    records = [Verdicts(SLUG, "intent", tmp_path).read(task["id"]) for task in doc["tasks"]]
    assert calls == []
    assert all(
        record and record["verdict"] == intent.PENDING and record["reason"] == intent.DRAFT for record in records
    )


@pytest.mark.feature("swarm-intent-check-budget")
@pytest.mark.parametrize("mode", ["coach", "observe"])
def test_a_later_pass_over_draft_pulls_rewrites_no_verdict(tmp_path: Any, monkeypatch: Any, mode: str) -> None:
    doc = _doc()
    pulls, _ = _pulls(doc, draft=True)
    _check(mode, pulls, [], tmp_path).run(doc)
    before = [Verdicts(SLUG, "intent", tmp_path).read(task["id"]) for task in doc["tasks"]]
    writes: list[str] = []
    monkeypatch.setattr(Verdicts, "write", lambda self, subject, *args, **fields: writes.append(subject))
    later, _ = _pulls(doc, draft=True)
    _check(mode, later, [], tmp_path, now=NOW + intent.TURN_MS).run(doc)
    assert writes == []
    assert [Verdicts(SLUG, "intent", tmp_path).read(task["id"]) for task in doc["tasks"]] == before


@pytest.mark.feature("swarm-intent-check-budget")
def test_a_draft_pull_that_turns_ready_is_marked_running_and_judged(tmp_path: Any) -> None:
    doc = {**_doc(), "tasks": _doc()["tasks"][:1]}
    pulls, _ = _pulls(doc, draft=True)
    _check("observe", pulls, [], tmp_path).run(doc)
    ready, _ = _pulls(doc, draft=False)
    calls: list[str] = []
    _check("observe", ready, calls, tmp_path, now=NOW + intent.TURN_MS).run(doc)
    record = Verdicts(SLUG, "intent", tmp_path).read("t0")
    assert "ask" in calls
    assert record and record["reason"] != intent.DRAFT


def _watched_doc() -> dict[str, Any]:
    """A pull in pr, a claimed pull, an open pull and a pr task without a pull."""
    tasks = [
        {"id": "a", "state": "pr", "pr_url": "https://github.com/o/r/pull/1"},
        {"id": "b", "state": "claimed", "pr_url": "https://github.com/o/r/pull/2"},
        {"id": "c", "state": "open", "pr_url": "https://github.com/o/r/pull/3"},
        {"id": "d", "state": "pr"},
    ]
    return {**DOC, "tasks": tasks}


@pytest.mark.feature("swarm-intent-check-budget")
@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("coach", ["pull/1", "pull/2", "pull/9", "pull/8"]),
        ("enforce", ["pull/1", "pull/9"]),
    ],
)
def test_watched_urls_reads_each_watched_pull_and_the_pending_pulls_both_verdict_stores_name(
    tmp_path: Any, mode: str, expected: list[str]
) -> None:
    base = "https://github.com/o/r/"
    Verdicts(SLUG, "intent", tmp_path).write("a", intent.PENDING, "r", named_pending=[base + "pull/9"])
    Verdicts(SLUG, "intent-coach", tmp_path).write("b", intent.PENDING, "r", named_pending=[base + "pull/8"])
    Verdicts(SLUG, "intent-coach", tmp_path).write("a", intent.PENDING, "r", named_pending=[base + "pull/1"])
    assert intent.watched_urls(SLUG, _watched_doc(), mode, tmp_path) == [base + path for path in expected]


@pytest.mark.feature("swarm-intent-check-budget")
def test_a_pass_cancels_the_judgment_of_a_task_it_no_longer_watches(tmp_path: Any) -> None:
    doc = _doc()
    pulls, _ = _pulls(doc)
    judging = intent.Judging()
    gone: futures.Future[Any] = futures.Future()
    judging.running[(SLUG, "gone")] = ("key", gone)
    replace(_check("coach", pulls, [], tmp_path), judging=judging).run(doc)
    assert (SLUG, "gone") not in judging.running
    assert gone.cancelled()


@pytest.mark.feature("swarm-intent-check-budget")
@pytest.mark.parametrize("minute", [0, 7, 29])
def test_each_minute_judges_the_changed_pulls_from_its_own_turn(tmp_path: Any, minute: int) -> None:
    doc = _doc()
    pulls, _ = _pulls(doc)
    now = minute * intent.TURN_MS
    _check("coach", pulls, [], tmp_path, now).run(doc)
    ids = [task["id"] for task in doc["tasks"]]
    judged = {task for task in ids if Verdicts(SLUG, "intent", tmp_path).read(task)}
    assert judged == set((ids[minute:] + ids[:minute])[: intent.JUDGED_PER_PASS])


@pytest.mark.feature("swarm-intent-check-budget")
def test_a_kept_judging_settles_draft_pulls_inside_the_pass(tmp_path: Any) -> None:
    doc = _doc()
    pulls, _ = _pulls(doc, draft=True)
    judging = intent.Judging()
    replace(_check("coach", pulls, [], tmp_path), judging=judging).run(doc)
    records = [Verdicts(SLUG, "intent", tmp_path).read(task["id"]) for task in doc["tasks"]]
    assert judging.running == {}
    assert all(record and record["reason"] == intent.DRAFT for record in records)
