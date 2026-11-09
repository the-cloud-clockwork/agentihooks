"""The intent check stays inside its tick step budget with thirty tasks in pr."""

import threading
from concurrent import futures
from dataclasses import replace
from typing import Any

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
