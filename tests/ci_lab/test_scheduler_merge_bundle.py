"""Mutation pass B of pull request 14018 (spec issue 13890): kills the survivors of run 46060233146 in
scripts/ci_lab/scheduler.py for BusInbox.heads' pull request number, listen's bundle port wiring and
MirrorExpander.bundle.
"""

import base64
import json
import subprocess
import tempfile
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from scripts.ci_lab import budget, lease_api, scheduler
from scripts.ci_lab.digests import Toolchain
from scripts.ci_lab.webhooks import HeadEvent, envelope
from scripts.dep_manifest import _git
from tests.ci_lab.test_scheduler import (
    ACTOR,
    BASE,
    HEAD_A,
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

MERGE = "7" * 40
PARENT = "6" * 40
BUNDLED = b"bundled-bytes"
BOGUS_SHA = "deadbeef" * 5


def _pr(run: Run, head: str, number: int, lane: str) -> None:
    event = HeadEvent("pull_request", REPO, head, BASE, ACTOR, "opened", lane, number)
    run.lab.inbox.pending.append(scheduler.Head(event, lane))


def test_bus_inbox_reads_the_pull_request_number_a_head_names() -> None:
    numbered = HeadEvent("pull_request", REPO, HEAD_A, BASE, ACTOR, "opened", pull_number=41)
    inbox = scheduler.BusInbox(Consumer([Delivery(envelope(numbered))]))
    assert inbox.heads() == [scheduler.Head(numbered, HEAD_A)]


def test_listen_wires_the_scheduler_s_own_bundle_port_into_the_bundle_route(lab: Callable[..., Run]) -> None:
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
