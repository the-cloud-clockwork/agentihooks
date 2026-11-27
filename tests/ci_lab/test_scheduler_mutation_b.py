"""Mutation pass B of pull request 14018 (spec issue 13890): kills the survivors of run 46060233146 in
scripts/ci_lab/scheduler.py for MirrorExpander.bundle, the lease and bus intake wiring, and the request, hold,
sibling and regrade paths of Scheduler.
"""

import base64
import json
import subprocess
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from scripts.ci_lab import budget, lease_api, scheduler
from scripts.ci_lab.digests import Toolchain
from scripts.ci_lab.intake import GradeRequest
from scripts.ci_lab.units import Priority
from scripts.ci_lab.webhooks import NOTICE, PUSHED, HeadEvent, envelope
from scripts.dep_manifest import _git
from tests.ci_lab.test_scheduler import (
    ACTOR,
    BASE,
    HEAD_A,
    HEAD_B,
    REPO,
    TREE,
    Consumer,
    Delivery,
    Run,
    _commit,
    _no_tests,
    _read,
    _unknown,
    unit,
)
from tests.ci_lab.test_scheduler import lab as lab

OTHER_HEAD = "f" * 40
BOGUS_SHA = "deadbeef" * 5
MERGE = "7" * 40
PARENT = "6" * 40
BUNDLED = b"bundled-bytes"


def _pr(run: Run, head: str, number: int, lane: str) -> None:
    event = HeadEvent("pull_request", REPO, head, BASE, ACTOR, "opened", lane, number)
    run.lab.inbox.pending.append(scheduler.Head(event, lane))


def test_request_defaults_the_pull_number_when_the_held_entry_names_none() -> None:
    data = {
        "repo": REPO,
        "head": HEAD_A,
        "base": BASE,
        "tier": int(Priority.PULL_REQUEST),
        "actor": ACTOR,
        "bypass_negative": False,
    }
    assert scheduler._request(data).pull_number == 0


def test_hold_leaves_a_waiting_entry_that_already_names_a_pull_request(lab: Any) -> None:
    run = lab({}, quiet_ms=10_000)
    _pr(run, HEAD_A, 41, HEAD_A)
    run.tick(1000)
    name = f"{REPO}|{HEAD_A}|{int(Priority.PULL_REQUEST)}"
    assert json.loads(run.lab.client.hget(scheduler.WAITING, name))["request"]["pull_number"] == 41
    _pr(run, HEAD_A, 55, "other-lane")
    run.tick(1100)
    held = run.lab.client.hget(scheduler.WAITING, name)
    assert held is not None
    assert json.loads(held)["request"]["pull_number"] == 41


def test_hold_defers_to_a_covering_pull_request_entry_found_past_an_unrelated_one(lab: Any) -> None:
    run = lab({}, quiet_ms=10_000)
    _pr(run, OTHER_HEAD, 7, OTHER_HEAD)
    _pr(run, HEAD_B, 41, "engineer-x")
    run.tick(1000)
    notice = HeadEvent(NOTICE, REPO, HEAD_B, BASE, ACTOR, PUSHED)
    run.lab.inbox.pending.append(scheduler.Head(notice, "feature"))
    actions = run.tick(1100)
    name = f"{REPO}|engineer-x|{int(Priority.PULL_REQUEST)}"
    assert f"left {HEAD_B} to pull request 41 waiting in {name}" in actions


def test_siblings_keeps_looking_past_a_live_grade_of_a_different_head(lab: Any) -> None:
    plans = {
        HEAD_A: scheduler.Plan(TREE, BASE, {"lint": (unit(1),)}),
        HEAD_B: scheduler.Plan(TREE, BASE, {"lint": (unit(2),)}),
    }
    run = lab(plans)
    run.push(HEAD_B, lane="aa-other", event="pull_request", action="opened")
    run.push(HEAD_A, lane="zz-head-a", event="pull_request", action="opened")
    run.tick(1000)
    request = GradeRequest(REPO, HEAD_A, BASE, Priority.PULL_REQUEST, ACTOR, False)
    siblings = list(run.engine._siblings(request))
    assert [state.run for state in siblings] == [run.grade(HEAD_A).run]


def test_siblings_skips_a_grade_ref_or_superseded_sibling_without_stopping_at_it(lab: Any) -> None:
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, {"lint": (unit(1),)})})
    run.push(HEAD_A, lane="template")
    run.tick(2000)
    template = run.grade(HEAD_A)
    client = run.lab.client
    client.srem(scheduler.LIVE, template.run)
    client.hdel(scheduler.ORIGINS, template.run)
    client.delete(scheduler.GRADES + template.run)
    tier = int(Priority.PULL_REQUEST)
    entries = (
        (replace(template, run="bad-ref", superseded=False), "refs/grade/x"),
        (replace(template, run="good", superseded=False), ""),
        (replace(template, run="superseded", superseded=True), ""),
    )
    for state, ref in entries:
        run.engine.save(state)
        client.sadd(scheduler.LIVE, state.run)
        client.hset(scheduler.ORIGINS, state.run, json.dumps({"tier": tier, "repo": REPO, "head": HEAD_A, "ref": ref}))
    request = GradeRequest(REPO, HEAD_A, BASE, Priority.PULL_REQUEST, ACTOR, False)
    siblings = list(run.engine._siblings(request))
    assert [state.run for state in siblings] == ["good"]


def test_regrade_clears_the_waiting_entry_its_own_name_identifies(lab: Any) -> None:
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, {"lint": (unit(1),)})})
    run.push(HEAD_A)
    run.tick(1000)
    state = run.grade(HEAD_A)
    run.lab.client.hset(scheduler.WAITING, "", json.dumps({"marker": "stale"}))
    request = GradeRequest(
        state.repo, state.head, state.base, Priority(state.tier), ACTOR, False, pull_number=state.pull_number
    )
    run.engine._regrade(state, request, 2000)
    assert run.lab.client.hexists(scheduler.WAITING, "") is False


def test_output_keeps_the_required_check_s_unit_count_before_the_fallback_line(lab: Any) -> None:
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, {"lint": (unit(1),)})})
    run.engine.ports = replace(run.engine.ports, merges=Mock(return_value=None))
    _pr(run, HEAD_A, 41, HEAD_A)
    for _ in range(scheduler.UNMERGED_TRIES + 1):
        run.tick(1000)
    posted = [post for post in run.lab.poster.posts if post[0] == HEAD_A and post[1] == scheduler.REQUIRED]
    assert posted
    output = posted[-1][4]
    assert output.summary.startswith(f"1 units of {scheduler.REQUIRED} on {HEAD_A} against {BASE}")
    assert "The lab got no test merge" in output.summary


def test_bus_inbox_reads_the_pull_request_number_a_head_names() -> None:
    numbered = HeadEvent("pull_request", REPO, HEAD_A, BASE, ACTOR, "opened", pull_number=41)
    inbox = scheduler.BusInbox(Consumer([Delivery(envelope(numbered))]))
    assert inbox.heads() == [scheduler.Head(numbered, HEAD_A)]


def test_listen_wires_the_scheduler_s_own_bundle_port_into_the_bundle_route(lab: Any) -> None:
    gates = {"lint": (unit(1),)}
    run = lab({HEAD_A: scheduler.Plan(TREE, BASE, gates, merge_base=PARENT, graded_commit=MERGE)})
    run.engine.ports = replace(
        run.engine.ports,
        merges=Mock(return_value=(MERGE, PARENT)),
        bundle=lambda sha: BUNDLED if sha == MERGE else None,
    )
    _pr(run, HEAD_A, 41, HEAD_A)
    run.tick(1000)
    spending = budget.Budget(run.lab.client, lambda: 1000, budget.Policy(100.0, {}))
    routes = lease_api.ControlRoutes(run.lab.client, "tok", lambda: 1000)
    servers = scheduler.listen(run.engine, run.leases(), routes, spending, lambda: 1000, (0, 0, 0))
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    api = f"http://127.0.0.1:{servers[0].server_address[1]}"
    try:
        body = json.dumps({"worker": "w", "free_slots": 1, "capabilities": [], "wait_ms": 0}).encode()
        leased = json.loads(_read(f"{api}/lease", body))["leases"]
        job, token = leased[0]["job"], leased[0]["token"]
        reply = json.loads(_read(f"{api}/bundle", json.dumps({"job": job, "token": token}).encode()))
        assert reply == {"bundle": base64.b64encode(BUNDLED).decode()}
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def _store(tmp_path: Path, root: Path, name: str) -> Path:
    store = root / name
    _git(tmp_path, "init", "-q", str(store))
    return store


def _expander(tmp_path: Path, root: Path) -> scheduler.MirrorExpander:
    toolchain = Toolchain("3.12", {}, "", "", "image", {})
    return scheduler.MirrorExpander(tmp_path / "node", root, toolchain, _no_tests, _unknown)


def test_bundle_returns_the_commit_with_its_parent_excluded_past_an_unrelated_store(tmp_path: Path) -> None:
    root = tmp_path / "stores"
    _store(tmp_path, root, "a-store")
    store_b = _store(tmp_path, root, "b-store")
    parent = _commit(store_b, {"a.txt": "1\n"})
    sha = _commit(store_b, {"a.txt": "2\n"})
    expander = _expander(tmp_path, root)

    found = expander.bundle(sha)

    assert found is not None
    (tmp_path / "out.bundle").write_bytes(found)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "--bare", str(tmp_path / "out.bundle"), str(clone))
    assert _git(clone, "cat-file", "-t", sha).strip() == "commit"
    excluded = subprocess.run(["git", "-C", str(clone), "cat-file", "-e", parent], capture_output=True)
    assert excluded.returncode != 0
    assert _git(store_b, "for-each-ref", "refs/lab-bundle/").strip() == ""


def test_bundle_raises_when_the_parent_read_of_the_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "stores"
    store = _store(tmp_path, root, "only-store")
    sha = _commit(store, {"a.txt": "1\n"})
    expander = _expander(tmp_path, root)
    real_run = subprocess.run

    def broken(argv: list[str], **kwargs: Any) -> Any:
        if len(argv) > 3 and argv[3] == "rev-list":
            argv = [*argv[:-1], "not-a-real-revision"]
        return real_run(argv, **kwargs)

    monkeypatch.setattr(scheduler.subprocess, "run", broken)

    with pytest.raises(subprocess.CalledProcessError):
        expander.bundle(sha)
    assert _git(store, "for-each-ref", "refs/lab-bundle/").strip() == ""


def test_bundle_raises_when_marking_the_commit_for_the_bundle_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "stores"
    store = _store(tmp_path, root, "only-store")
    sha = _commit(store, {"a.txt": "1\n"})
    expander = _expander(tmp_path, root)
    real_run = subprocess.run

    def broken(argv: list[str], **kwargs: Any) -> Any:
        if len(argv) > 4 and argv[3] == "update-ref" and argv[4] != "-d":
            argv = [*argv[:-1], BOGUS_SHA]
        return real_run(argv, **kwargs)

    monkeypatch.setattr(scheduler.subprocess, "run", broken)

    with pytest.raises(subprocess.CalledProcessError) as failed:
        expander.bundle(sha)
    assert failed.value.stderr
    assert _git(store, "for-each-ref", "refs/lab-bundle/").strip() == ""


def test_bundle_names_its_temporary_file_with_the_bundle_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "stores"
    store = _store(tmp_path, root, "only-store")
    sha = _commit(store, {"a.txt": "1\n"})
    expander = _expander(tmp_path, root)
    seen: list[str | None] = []
    real_tempfile = tempfile.NamedTemporaryFile

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("suffix"))
        return real_tempfile(*args, **kwargs)

    monkeypatch.setattr(scheduler.tempfile, "NamedTemporaryFile", spy)

    assert expander.bundle(sha) is not None
    assert seen == [".bundle"]


def test_bundle_raises_when_creating_the_bundle_file_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "stores"
    store = _store(tmp_path, root, "only-store")
    sha = _commit(store, {"a.txt": "1\n"})
    expander = _expander(tmp_path, root)
    real_run = subprocess.run

    def broken(argv: list[str], **kwargs: Any) -> Any:
        if len(argv) > 4 and argv[3] == "bundle" and argv[4] == "create":
            argv = [*argv[:6], "refs/lab-bundle/does-not-exist", *argv[7:]]
        return real_run(argv, **kwargs)

    monkeypatch.setattr(scheduler.subprocess, "run", broken)

    with pytest.raises(subprocess.CalledProcessError) as failed:
        expander.bundle(sha)
    assert failed.value.stderr
    assert _git(store, "for-each-ref", "refs/lab-bundle/").strip() == ""


def test_bundle_raises_when_clearing_its_temporary_ref_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "stores"
    store = _store(tmp_path, root, "only-store")
    sha = _commit(store, {"a.txt": "1\n"})
    expander = _expander(tmp_path, root)
    real_run = subprocess.run
    missing = root / "missing-store"

    def broken(argv: list[str], **kwargs: Any) -> Any:
        if len(argv) > 4 and argv[3] == "update-ref" and argv[4] == "-d":
            argv = ["git", "-C", str(missing), *argv[3:]]
        return real_run(argv, **kwargs)

    monkeypatch.setattr(scheduler.subprocess, "run", broken)

    with pytest.raises(subprocess.CalledProcessError) as failed:
        expander.bundle(sha)
    assert failed.value.stderr
    assert _git(store, "for-each-ref", "refs/lab-bundle/").strip() != ""
