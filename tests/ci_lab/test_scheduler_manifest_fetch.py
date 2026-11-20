import io
import json
import threading
import zipfile
from collections.abc import Callable
from concurrent.futures import wait
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from scripts import github_app
from scripts.ci_lab import manifests, scheduler
from scripts.ci_lab.digests import Toolchain
from scripts.ci_lab.manifests import Actions, Fetcher
from scripts.ci_lab.objects import LocalObjects, manifest_folder
from scripts.ci_lab.units import Priority
from scripts.ci_lab.webhooks import HeadEvent
from scripts.dep_manifest import _git
from tests.ci_lab.test_scheduler import (
    ACTOR,
    DELIVERY,
    FIRST_TEST,
    HEAD_A,
    HEAD_B,
    REPO,
    SLUG,
    STORED_MANIFEST,
    Fetches,
    Port,
    Run,
    _suite,
    packed_file,
    plan,
    unit,
)
from tests.ci_lab.test_scheduler import lab as _lab
from tests.ci_lab.test_scheduler import staticless as _staticless

lab = _lab
staticless = _staticless


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
        for when, fetches in ((31_000, 1), (61_000, 2)):
            waiting = {name: json.loads(raw) for name, raw in run.lab.client.hgetall(scheduler.WAITING).items()}
            assert waiting == {
                name: {**value, "since": when, "unanswered": fetches} for name, value in original.items()
            }
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
