"""Scheduler test units for stored-manifest reads and GitHub manifest fetches."""

import copy
import io
import json
import threading
import zipfile
from collections.abc import Callable
from concurrent.futures import wait
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
import requests

from scripts import dep_manifest, github_app
from scripts.ci_lab import expand_tests, interaction, manifests, scheduler
from scripts.ci_lab.digests import Toolchain, digest
from scripts.ci_lab.manifests import Actions, Fetcher
from scripts.ci_lab.objects import LocalObjects, manifest_folder
from scripts.ci_lab.units import Priority, Unit
from scripts.ci_lab.webhooks import HeadEvent
from scripts.dep_manifest import _git
from tests.ci_lab.test_scheduler import (
    ACTOR,
    ALPHA,
    DELIVERY,
    FIRST_TEST,
    HEAD_A,
    HEAD_B,
    OTHER_TEST,
    REPO,
    SECOND_TEST,
    SLUG,
    STORED_MANIFEST,
    SUITE_MANIFEST,
    Port,
    Run,
    _suite,
    key,
    plan,
    unit,
)
from tests.ci_lab.test_scheduler import lab as lab
from tests.ci_lab.test_scheduler import staticless as staticless


def unfetched(slug: str, commit: str) -> bytes | None:
    return None


def packed_file(manifest: dict[str, Any], commit: str) -> bytes:
    found: dict[str, Any] = dep_manifest.pack({**manifest, "commit": commit})
    return json.dumps(found, separators=(",", ":")).encode()


@dataclass
class Fetches:
    found: dict[str, bytes]
    asked: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def __call__(self, slug: str, commit: str) -> bytes | None:
        self.asked.append((slug, commit))
        return self.found.get(commit)


@pytest.mark.feature("lab-scheduler-test-units")
def test_stored_manifests_read_the_packed_manifest_of_the_nearest_stored_ancestor(tmp_path: Path) -> None:
    objects = LocalObjects(tmp_path / "objects")
    for commit in ("older", "newer"):
        packed = dep_manifest.pack({**STORED_MANIFEST, "commit": commit})
        objects.write_once(f"{manifest_folder(SLUG)}{commit}.json", json.dumps(packed).encode())
    objects.write_once(f"{manifest_folder('other/repo')}head.json", b"{}")
    stored = scheduler.StoredManifests(objects, unfetched)
    assert stored(REPO, ["head", "newer", "older"]) == {**STORED_MANIFEST, "commit": "newer"}
    assert stored(REPO, ["head", "older"]) == {**STORED_MANIFEST, "commit": "older"}
    assert stored(REPO, ["head"]) is None
    assert manifest_folder(SLUG) == "lab/manifests/the-cloud-clockwork--agentihooks/"


@pytest.mark.feature("lab-verdict-outside-to-manifest")
def test_a_note_joins_the_outside_list_a_stored_or_fetched_record_already_holds(tmp_path: Path) -> None:
    objects = LocalObjects(tmp_path / "objects")
    objects.write_once(f"{manifest_folder(SLUG)}older.json", packed_file(SUITE_MANIFEST, "older"))
    stored = scheduler.StoredManifests(objects, Fetches({"newer": packed_file(SUITE_MANIFEST, "newer")}))
    assert stored(REPO, ["newer"]) == {**SUITE_MANIFEST, "commit": "newer"}
    for commit in ("newer", "older"):
        scheduler.note_outside(
            objects, REPO, commit, (SECOND_TEST, "tests/test_gone.py::test_gone"), ["docs/p.md", "docs/a.md"]
        )
    records = SUITE_MANIFEST["tests"]
    second = {**records[SECOND_TEST], "outside": ["docs/a.md", "docs/c.md", "docs/p.md"]}
    for commit in ("newer", "older"):
        assert stored(REPO, [commit]) == {**SUITE_MANIFEST, "commit": commit, "tests": {**records, SECOND_TEST: second}}


def _batch_keys(manifest: dict[str, Any] | None, note: bytes) -> dict[tuple[str, ...], str]:
    assert manifest is not None
    tree = {"scripts/alpha.py": ALPHA.encode(), "docs/p.md": note}
    blobs = {path: content.hex() for path, content in tree.items()}
    chain = Toolchain("3.12", {}, "", "", "image", {})
    return {batch.members: batch.key for batch in expand_tests.units(manifest, tree, blobs, {}, chain)}


@pytest.mark.feature("lab-verdict-outside-to-manifest")
def test_a_test_verdict_outside_list_reaches_the_stored_records_and_the_following_key(
    lab: Callable[..., Run],
) -> None:
    members = (OTHER_TEST, SECOND_TEST, "tests/test_gone.py::test_gone")
    first, twin, failed, plain = (
        Unit(expand_tests.KIND, key(expand_tests.KIND, number), tests)
        for number, tests in ((1, members), (2, members), (3, (FIRST_TEST,)), (4, (FIRST_TEST,)))
    )
    run = lab({HEAD_A: replace(plan({"unit": (first, twin, failed, plain)}), manifest="dev")})
    folder = manifest_folder(SLUG)
    packed = dep_manifest.pack(copy.deepcopy(SUITE_MANIFEST))
    run.lab.objects.write_once(f"{folder}dev.json", json.dumps(packed).encode())
    stored = scheduler.StoredManifests(run.lab.objects)
    before = stored(REPO, ["dev"])
    run.push(HEAD_A)
    run.tick(1000)
    grants = {grant.unit: grant for grant in run.lease(slots=4)}
    run.finish(grants[first], "passed", {"seconds": 1.0, "outside": ["docs/p.md", "docs/c.md"]})
    run.finish(grants[twin], "passed", {"seconds": 1.0, "outside": ["docs/c.md", "docs/p.md"]})
    run.finish(grants[failed], "failed", {"seconds": 1.0, "outside": ["docs/c.md"]})
    run.finish(grants[plain], "passed", {"seconds": 1.0, "outside": []})
    run.tick(1100)
    after = stored(REPO, ["dev"])
    records = SUITE_MANIFEST["tests"]
    both = ["docs/c.md", "docs/p.md"]
    assert before == SUITE_MANIFEST
    assert after == {
        **SUITE_MANIFEST,
        "tests": {
            FIRST_TEST: {**records[FIRST_TEST], "outside": ["docs/c.md"]},
            OTHER_TEST: {**records[OTHER_TEST], "outside": both},
            SECOND_TEST: {**records[SECOND_TEST], "outside": both},
        },
    }
    notes = [
        f"{folder}outside/dev/{digest({'tests': list(tests), 'outside': files})}.json"
        for tests, files in ((members, both), ((FIRST_TEST,), ["docs/c.md"]))
    ]
    assert run.lab.objects.keys(folder) == sorted([f"{folder}dev.json", *notes])
    change = [[("docs/p.md", "p\n", "q\n")]]
    assert before is not None and after is not None
    assert interaction.invalidated(before, change) == frozenset()
    assert interaction.invalidated(after, change) == {OTHER_TEST, SECOND_TEST}
    assert _batch_keys(before, b"p\n") == _batch_keys(before, b"q\n")
    assert _batch_keys(after, b"p\n")[(FIRST_TEST, OTHER_TEST)] != _batch_keys(after, b"q\n")[(FIRST_TEST, OTHER_TEST)]


@pytest.mark.feature("lab-outside-notes-per-commit")
def test_a_note_of_an_older_manifest_commit_leaves_a_newer_commit_key_unchanged(tmp_path: Path) -> None:
    objects = LocalObjects(tmp_path / "objects")
    folder = manifest_folder(SLUG)
    for commit in ("older", "newer"):
        objects.write_once(f"{folder}{commit}.json", packed_file(SUITE_MANIFEST, commit))
    note: dict[str, object] = {"tests": [FIRST_TEST, OTHER_TEST], "outside": ["docs/p.md"]}
    objects.write_once(f"{folder}outside/older/{digest(note)}.json", json.dumps(note).encode())
    stored = scheduler.StoredManifests(objects, unfetched)
    newer, older = stored(REPO, ["newer", "older"]), stored(REPO, ["older"])
    records = SUITE_MANIFEST["tests"]
    widened = {test: {**records[test], "outside": ["docs/p.md"]} for test in (FIRST_TEST, OTHER_TEST)}
    assert newer == {**SUITE_MANIFEST, "commit": "newer"}
    assert older == {**SUITE_MANIFEST, "commit": "older", "tests": {**records, **widened}}
    assert _batch_keys(newer, b"p\n") == _batch_keys(newer, b"q\n")
    assert _batch_keys(older, b"p\n")[(FIRST_TEST, OTHER_TEST)] != _batch_keys(older, b"q\n")[(FIRST_TEST, OTHER_TEST)]


@pytest.mark.feature("lab-outside-notes-per-commit")
def test_a_manifest_read_removes_the_notes_of_commits_with_no_stored_manifest(tmp_path: Path) -> None:
    objects = LocalObjects(tmp_path / "objects")
    folder, other = manifest_folder(SLUG), manifest_folder("other/repo")
    objects.write_once(f"{folder}kept.json", packed_file(STORED_MANIFEST, "kept"))
    note = json.dumps({"tests": [FIRST_TEST], "outside": ["docs/p.md"]}).encode()
    kept, gone, flat = f"{folder}outside/kept/n.json", f"{folder}outside/gone/n.json", f"{folder}outside/n.json"
    for path in (kept, gone, flat, f"{other}outside/gone/n.json"):
        objects.write_once(path, note)
    found = scheduler.StoredManifests(objects, unfetched)(REPO, ["kept"])
    first = {**STORED_MANIFEST["tests"][FIRST_TEST], "outside": ["docs/p.md"]}
    assert found == {**STORED_MANIFEST, "commit": "kept", "tests": {FIRST_TEST: first}}
    assert objects.keys(folder) == [f"{folder}kept.json", kept]
    assert objects.keys(other) == [f"{other}outside/gone/n.json"]
    assert scheduler.StoredManifests(objects, unfetched)(REPO, ["missing"]) is None
    assert objects.keys(folder) == [f"{folder}kept.json", kept]


@pytest.mark.feature("lab-outside-notes-per-commit")
def test_a_grade_that_keyed_no_manifest_writes_no_outside_note(lab: Callable[..., Run]) -> None:
    batch = Unit(expand_tests.KIND, key(expand_tests.KIND, 1), (FIRST_TEST,))
    run = lab({HEAD_A: plan({"unit": (batch,)})})
    run.push(HEAD_A)
    run.tick(1000)
    (grant,) = run.lease(slots=1)
    run.finish(grant, "passed", {"seconds": 1.0, "outside": ["docs/p.md"]})
    run.tick(1100)
    assert run.store.get(batch.kind, batch.key) is not None
    assert run.lab.objects.keys(manifest_folder(SLUG)) == []


@pytest.mark.feature("lab-dev-manifest-fetch")
def test_stored_manifests_fetch_a_newest_commit_stored_nowhere_and_keep_it_by_repository_and_commit(
    tmp_path: Path,
) -> None:
    objects = LocalObjects(tmp_path / "objects")
    objects.write_once(f"{manifest_folder(SLUG)}older.json", packed_file(STORED_MANIFEST, "older"))
    fetches = Fetches({"newer": packed_file(STORED_MANIFEST, "newer")})
    stored = scheduler.StoredManifests(objects, fetches)
    assert stored(REPO, ["head", "newer", "older"]) == {**STORED_MANIFEST, "commit": "newer"}
    assert fetches.asked == [(SLUG, "head"), (SLUG, "newer")]
    folder = "lab/manifests/the-cloud-clockwork--agentihooks/"
    assert objects.keys("lab/") == [f"{folder}newer.json", f"{folder}older.json"]
    assert objects.read(f"{folder}newer.json") == packed_file(STORED_MANIFEST, "newer")
    assert stored(REPO, ["newer", "older"]) == {**STORED_MANIFEST, "commit": "newer"}
    assert stored(REPO, ["older"]) == {**STORED_MANIFEST, "commit": "older"}
    assert fetches.asked == [(SLUG, "head"), (SLUG, "newer")]


@pytest.mark.feature("lab-dev-manifest-fetch")
def test_stored_manifests_fetch_only_the_newest_commits_of_the_chain(tmp_path: Path) -> None:
    objects = LocalObjects(tmp_path / "objects")
    commits = [f"commit-{index}" for index in range(scheduler.MANIFEST_DEPTH + 1)]
    fetches = Fetches({commits[-1]: packed_file(STORED_MANIFEST, commits[-1])})
    assert scheduler.StoredManifests(objects, fetches)(REPO, commits) is None
    assert fetches.asked == [(SLUG, commit) for commit in commits[:-1]]
    assert objects.keys("lab/") == []


@pytest.mark.feature("lab-dev-manifest-fetch")
@pytest.mark.usefixtures("staticless")
def test_a_head_that_needs_test_units_expands_on_the_manifest_stored_from_its_base_dev_push_run(
    tmp_path: Path,
) -> None:
    suite = _suite(tmp_path, DELIVERY["lab"])
    objects = LocalObjects(tmp_path / "objects")
    fetches = Fetches({suite.base: packed_file(STORED_MANIFEST, suite.base)})
    node = tmp_path / "node"
    _git(tmp_path, "init", "-q", "--bare", str(node))
    toolchain = Toolchain("3.12", {}, "", "", "image", {})
    port = Port()
    stored = scheduler.StoredManifests(objects, fetches)
    expander = scheduler.MirrorExpander(node, tmp_path / "stores", toolchain, stored, port.verdicts)
    found = expander.expand(suite.request(Priority.PROMOTION))
    assert [batch.members for batch in found.gates["unit"]] == [(FIRST_TEST,)]
    assert found.manifest == suite.base
    slug = str(suite.repo).removeprefix("/")
    assert fetches.asked == [(slug, suite.base)]
    assert objects.read(f"{manifest_folder(slug)}{suite.base}.json") == packed_file(STORED_MANIFEST, suite.base)


@pytest.mark.feature("lab-manifest-fetch-holds-head")
@pytest.mark.usefixtures("staticless")
def test_a_head_whose_manifest_fetch_times_out_waits_out_its_window_again_and_a_later_pass_grades_it(
    lab: Callable[..., Run], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = _suite(tmp_path, DELIVERY["lab"])
    packed = packed_file(STORED_MANIFEST, suite.base)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as written:
        written.writestr("packed.json", packed)
    get = Mock(
        side_effect=[
            requests.ReadTimeout("read timed out"),
            Mock(status_code=200, json=Mock(return_value={"workflow_runs": [{"id": 1}]})),
            Mock(status_code=200, json=Mock(return_value={"artifacts": [{"id": 2, "expired": False}]})),
            Mock(status_code=200, content=archive.getvalue()),
        ]
    )
    monkeypatch.setattr(requests, "get", get)
    run = lab({}, quiet_ms=30_000)
    token = Mock(get_token=Mock(return_value="fixture"))
    stored = scheduler.StoredManifests(run.lab.objects, Fetcher(lambda repo: Actions(repo, token)))
    node = tmp_path / "node"
    _git(tmp_path, "init", "-q", "--bare", str(node))
    toolchain = Toolchain("3.12", {}, "", "", "image", {})
    expander = scheduler.MirrorExpander(node, tmp_path / "stores", toolchain, stored, Port().verdicts)
    monkeypatch.setattr(run.lab.expander, "expand", expander.expand)
    event = HeadEvent("push", suite.remote, suite.head, suite.base, ACTOR, "pushed")
    run.lab.inbox.pending.append(scheduler.Head(event, "dev"))
    assert run.tick(1000) == [f"received {suite.head} on dev"]
    runs = f"actions/workflows/test.yml/runs?event=push&branch=dev&head_sha={suite.base}"
    assert run.tick(31_000) == [f"holding {suite.head}: GET {runs} failed: read timed out"]
    assert run.tick(60_999) == []
    assert get.call_count == 1
    assert run.tick(61_000)[0].startswith("grading ")
    assert run.grade(suite.head).base == suite.base
    slug = str(suite.repo).removeprefix("/")
    assert run.lab.objects.read(f"{manifest_folder(slug)}{suite.base}.json") == packed
    assert get.call_count == 4


@pytest.mark.feature("lab-token-mint-status")
@pytest.mark.usefixtures("staticless")
@pytest.mark.parametrize(("status", "held"), [(None, True), (403, False), (499, False), (500, True), (599, True)])
def test_manifest_token_mint_failures_hold_only_transient_heads_and_grade_after_recovery(
    lab: Callable[..., Run], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int | None, held: bool
) -> None:
    suite = _suite(tmp_path, DELIVERY["lab"])
    packed = packed_file(STORED_MANIFEST, suite.base)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as written:
        written.writestr("packed.json", packed)
    get = Mock(
        side_effect=[
            Mock(status_code=200, json=Mock(return_value={"workflow_runs": [{"id": 1}]})),
            Mock(status_code=200, json=Mock(return_value={"artifacts": [{"id": 2, "expired": False}]})),
            Mock(status_code=200, content=archive.getvalue()),
        ]
    )
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "fixture")
    monkeypatch.setattr(github_app.jwt, "encode", Mock(return_value="signed"))
    failed = requests.ReadTimeout("mint timed out") if status is None else Mock(status_code=status)
    granted = Mock(
        status_code=201,
        json=Mock(
            return_value={
                "token": "fixture",
                "expires_at": "2099-01-01T00:00:00Z",
                "repositories": [{"full_name": "example/repo"}],
                "permissions": {"actions": "read"},
            }
        ),
    )
    mint = Mock(side_effect=[failed, failed, granted])
    monkeypatch.setattr(requests, "post", mint)
    run = lab({}, quiet_ms=30_000)
    token = github_app.GitHubAppAuth("github.com/example/repo", permissions={"actions": "read"})
    stored = scheduler.StoredManifests(run.lab.objects, Fetcher(lambda repo: Actions(repo, token)))
    node = tmp_path / "node"
    _git(tmp_path, "init", "-q", "--bare", str(node))
    toolchain = Toolchain("3.12", {}, "", "", "image", {})
    expander = scheduler.MirrorExpander(node, tmp_path / "stores", toolchain, stored, Port().verdicts)
    monkeypatch.setattr(run.lab.expander, "expand", expander.expand)
    event = HeadEvent("push", suite.remote, suite.head, suite.base, ACTOR, "pushed")
    run.lab.inbox.pending.append(scheduler.Head(event, "dev"))
    assert run.tick(1000) == [f"received {suite.head} on dev"]
    original = {name: json.loads(raw) for name, raw in run.lab.client.hgetall(scheduler.WAITING).items()}
    runs = f"actions/workflows/test.yml/runs?event=push&branch=dev&head_sha={suite.base}"
    failure = "failed: mint timed out" if status is None else f"token mint failed: {github_app.REFUSED}"
    verb = "holding" if held else "cannot expand"
    assert run.tick(31_000) == [f"{verb} {suite.head}: GET {runs} {failure}"]
    get.assert_not_called()
    assert mint.call_count == 1
    assert run.grades() == []
    if not held:
        assert run.lab.client.hgetall(scheduler.WAITING) == {}
        assert run.tick(61_000) == []
        assert mint.call_count == 1
    else:
        for when in (31_000, 61_000):
            waiting = {name: json.loads(raw) for name, raw in run.lab.client.hgetall(scheduler.WAITING).items()}
            assert waiting == {name: {**value, "since": when} for name, value in original.items()}
            assert run.tick(when + 29_999) == []
            assert mint.call_count == (1 if when == 31_000 else 2)
            if when == 31_000:
                assert run.tick(61_000) == [f"holding {suite.head}: GET {runs} {failure}"]
        assert run.tick(91_000)[0].startswith("grading ")
        assert run.grade(suite.head).base == suite.base
        assert run.lab.client.hgetall(scheduler.WAITING) == {}
        slug = str(suite.repo).removeprefix("/")
        assert run.lab.objects.read(f"{manifest_folder(slug)}{suite.base}.json") == packed
        assert mint.call_count == 3
        assert get.call_count == 3


@pytest.mark.wall_clock
@pytest.mark.feature("lab-manifest-fetch-off-pass")
@pytest.mark.usefixtures("staticless")
def test_a_pass_reaps_leases_and_advances_grades_while_a_manifest_fetch_github_never_answers(
    lab: Callable[..., Run], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = _suite(tmp_path, DELIVERY["lab"])
    answered = threading.Event()

    def never_answers(fetcher: Fetcher, slug: str, commit: str) -> bytes | None:
        answered.wait(2 * manifests.TIMEOUT)
        return packed_file(STORED_MANIFEST, commit)

    monkeypatch.setattr(Fetcher, "__call__", never_answers)
    lost, done = unit(1), unit(2)
    run = lab({HEAD_A: plan({"lint": (lost,)}), HEAD_B: plan({"lint": (done,)})})
    run.push(HEAD_A, lane="lost")
    run.push(HEAD_B, lane="done")
    run.tick(1000)
    grants = {grant.unit: grant for grant in run.lease(slots=2, ttl_ms=100)}
    run.finish(grants[done], "passed", {})
    node = tmp_path / "node"
    _git(tmp_path, "init", "-q", "--bare", str(node))
    toolchain = Toolchain("3.12", {}, "", "", "image", {})
    stored = scheduler.StoredManifests(run.lab.objects)
    expander = scheduler.MirrorExpander(node, tmp_path / "stores", toolchain, stored, Port().verdicts)
    monkeypatch.setattr(run.lab.expander, "expand", expander.expand)
    event = HeadEvent("push", suite.remote, suite.head, suite.base, ACTOR, "pushed")
    run.lab.inbox.pending.append(scheduler.Head(event, "dev"))
    passes: list[list[str]] = []
    ticking = threading.Thread(target=lambda: passes.append(run.tick(1100)), daemon=True)
    ticking.start()
    ticking.join(manifests.TIMEOUT)
    stalled = ticking.is_alive()
    answered.set()
    ticking.join()
    assert not stalled
    [actions] = passes
    slug = str(suite.repo).removeprefix("/")
    assert f"holding {suite.head}: {slug} manifest of {suite.base} is still fetching" in actions
    assert f"reaped {grants[lost].job} pending" in actions
    assert f"completed {run.grade(HEAD_B).run} success" in actions
    assert isinstance(stored.fetch, manifests.Background)
    wait([future for _, future in stored.fetch.fetches.values()])
    assert run.tick(1200)[0].startswith("grading ")
    assert run.grade(suite.head).base == suite.base
