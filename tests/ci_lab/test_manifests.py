"""Tests for the lab's dev manifest fetch: the dev push Tests run of a commit, its packed artifact and the App token."""

import io
import json
import threading
import zipfile
from collections.abc import Mapping
from concurrent.futures import Future, wait
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import Mock, call

import pytest
import requests

from scripts import github_app
from scripts.ci_lab import manifests
from scripts.ci_lab.manifests import Actions, Fetcher, FetchRefused, FetchUnanswered

COMMIT = "c" * 40
OTHER = "d" * 40
COMMIT_RUNS_PATH = f"actions/workflows/test.yml/runs?event=push&branch=dev&head_sha={COMMIT}"
URL = "https://api.github.com/repos/example/repo/"
HEADERS = {
    "Authorization": "Bearer fixture",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def listed(run: int) -> str:
    """Return the path that lists a run's packed manifest artifact."""
    return f"actions/runs/{run}/artifacts?name=dep-manifest-packed"


def zipped(number: int) -> str:
    """Return the path that downloads an artifact's archive."""
    return f"actions/artifacts/{number}/zip"


def packed(commit: str) -> bytes:
    """Return a packed manifest file of a commit the way the dev push Tests job writes it."""
    manifest = {"version": 2, "commit": commit, "units": [], "tests": {}, "files": [], "spans": []}
    return json.dumps(manifest, separators=(",", ":")).encode()


def archive(files: Mapping[str, bytes]) -> bytes:
    """Return a zip archive holding the files."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as written:
        for name, data in files.items():
            written.writestr(name, data)
    return buffer.getvalue()


def artifact(number: int, expired: bool = False) -> dict[str, Any]:
    """Return a run artifact listing entry of the packed manifest."""
    return {"id": number, "name": "dep-manifest-packed", "expired": expired}


@dataclass
class FakeGitHub:
    """A repository's Actions API answering canned listings and archives, recording each path it is asked for."""

    listings: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    archives: dict[str, bytes | None] = field(default_factory=dict[str, bytes | None])
    asked: list[str] = field(default_factory=list[str])

    def json(self, path: str) -> dict[str, Any]:
        """Return the listing of a path and query."""
        self.asked.append(path)
        return self.listings[path]

    def archive(self, path: str) -> bytes | None:
        """Return the archive of a path, None when it is gone."""
        self.asked.append(path)
        return self.archives[path]


def github(
    runs: list[int], artifacts: Mapping[int, list[dict[str, Any]]], archives: Mapping[int, bytes | None]
) -> FakeGitHub:
    """Return a fake API whose commit has the dev push runs, each with its artifacts and their archives."""
    fake = FakeGitHub()
    fake.listings[COMMIT_RUNS_PATH] = {"workflow_runs": [{"id": run} for run in runs]}
    for run, found in artifacts.items():
        fake.listings[listed(run)] = {"artifacts": found}
    for number, data in archives.items():
        fake.archives[zipped(number)] = data
    return fake


class Token:
    """A token source that hands out the fixture token and counts how often it was asked."""

    def __init__(self) -> None:
        self.minted = 0

    def get_token(self) -> str:
        """Return the fixture token."""
        self.minted += 1
        return "fixture"


class Unminted:
    """A token source whose mint never gets an answer."""

    def get_token(self) -> str:
        """Raise the error requests raises for a token mint that gets no answer."""
        raise requests.ConnectionError("mint reset")


def answer(status: int, body: Mapping[str, Any] | None = None, content: bytes = b"") -> Mock:
    """Return a mocked HTTP response."""
    return Mock(status_code=status, json=Mock(return_value=dict(body or {})), content=content)


@pytest.mark.feature("lab-dev-manifest-fetch")
def test_fetch_answers_the_packed_manifest_the_dev_push_tests_run_of_the_commit_kept() -> None:
    fake = github([7], {7: [artifact(9)]}, {9: archive({"packed.json": packed(COMMIT)})})
    assert manifests.fetch(fake, COMMIT) == packed(COMMIT)
    assert fake.asked == [COMMIT_RUNS_PATH, listed(7), zipped(9)]


@pytest.mark.feature("lab-dev-manifest-fetch")
@pytest.mark.parametrize(("runs", "artifacts"), [([], {}), ([7], {7: []})], ids=["no-run", "no-artifact"])
def test_fetch_answers_none_for_a_commit_whose_dev_push_run_kept_no_manifest(
    runs: list[int], artifacts: dict[int, list[dict[str, Any]]]
) -> None:
    fake = github(runs, artifacts, {})
    assert manifests.fetch(fake, COMMIT) is None
    assert fake.asked == [COMMIT_RUNS_PATH, *map(listed, runs)]


@pytest.mark.feature("lab-dev-manifest-fetch")
def test_fetch_never_downloads_an_expired_artifact_and_answers_none_for_one_whose_archive_is_gone() -> None:
    fake = github([7], {7: [artifact(8, expired=True), artifact(9)]}, {9: None})
    assert manifests.fetch(fake, COMMIT) is None
    assert fake.asked == [COMMIT_RUNS_PATH, listed(7), zipped(9)]


@pytest.mark.feature("lab-dev-manifest-fetch")
def test_fetch_passes_over_dead_artifacts_and_another_commits_manifest_to_the_next_live_one() -> None:
    other = archive({"packed.json": packed(OTHER)})
    found = archive({"packed.json": packed(COMMIT)})
    runs = {6: [artifact(8, expired=True), artifact(9)], 7: [artifact(10), artifact(11)]}
    fake = github([6, 7], runs, {9: None, 10: other, 11: found})
    assert manifests.fetch(fake, COMMIT) == packed(COMMIT)
    assert fake.asked == [COMMIT_RUNS_PATH, listed(6), zipped(9), listed(7), zipped(10), zipped(11)]


@pytest.mark.feature("lab-dev-manifest-fetch")
@pytest.mark.parametrize(
    "data",
    [
        b"torn",
        archive({"manifest.json": packed(COMMIT)}),
        archive({"packed.json": b"{"}),
        archive({"packed.json": b"{}"}),
        archive({"packed.json": b"[]"}),
    ],
    ids=["torn", "unpacked", "not-json", "no-commit", "not-an-object"],
)
def test_fetch_refuses_an_archive_without_a_readable_packed_manifest(data: bytes) -> None:
    fake = github([7], {7: [artifact(9)]}, {9: data})
    with pytest.raises(FetchRefused, match="^actions/artifacts/9/zip holds no readable packed.json$"):
        manifests.fetch(fake, COMMIT)


@pytest.mark.feature("lab-dev-manifest-fetch")
def test_the_lab_reads_the_actions_api_with_a_token_that_carries_only_actions_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "fixture")
    monkeypatch.setattr(github_app.jwt, "encode", Mock(return_value="signed"))
    granted = {
        "token": "lab-token",
        "expires_at": "2099-01-01T00:00:00Z",
        "permissions": {"actions": "read", "metadata": "read"},
        "repositories": [{"full_name": "example/repo"}],
    }
    mint = Mock(return_value=answer(201, granted))
    monkeypatch.setattr(requests, "post", mint)
    get = Mock(return_value=answer(200, {"workflow_runs": []}))
    monkeypatch.setattr(requests, "get", get)
    assert Actions("example/repo").json(COMMIT_RUNS_PATH) == {"workflow_runs": []}
    assert mint.call_args.kwargs["json"] == {"repositories": ["repo"], "permissions": {"actions": "read"}}
    assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer lab-token"


@pytest.mark.feature("lab-dev-manifest-fetch")
def test_actions_answers_none_for_a_gone_archive_and_names_any_other_refused_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = [
        answer(200, {"artifacts": []}),
        answer(200, content=b"zip"),
        answer(410),
        answer(404),
        answer(502),
        requests.ConnectionError("reset"),
    ]
    get = Mock(side_effect=answers)
    monkeypatch.setattr(requests, "get", get)
    token = Token()
    actions = Actions("example/repo", token)
    assert actions.json(listed(7)) == {"artifacts": []}
    assert actions.archive(zipped(9)) == b"zip"
    assert actions.archive(zipped(9)) is None
    with pytest.raises(FetchRefused, match="^GET actions/artifacts/9/zip answered 404$"):
        actions.archive(zipped(9))
    with pytest.raises(FetchRefused, match=f"^GET actions/workflows/test.yml/runs\\?.*={COMMIT} answered 502$"):
        actions.json(COMMIT_RUNS_PATH)
    with pytest.raises(FetchRefused, match="^GET actions/artifacts/9/zip failed: reset$"):
        actions.archive(zipped(9))
    with pytest.raises(FetchRefused, match="^GET actions/artifacts/9/zip failed: mint reset$"):
        Actions("example/repo", Unminted()).archive(zipped(9))
    lists = call(f"{URL}{listed(7)}", headers=HEADERS, timeout=30)
    downloads = call(f"{URL}{zipped(9)}", headers=HEADERS, timeout=30)
    runs = call(f"{URL}{COMMIT_RUNS_PATH}", headers=HEADERS, timeout=30)
    assert get.call_args_list == [lists, downloads, downloads, downloads, runs, downloads]
    assert token.minted == len(answers)


@pytest.mark.feature("lab-manifest-fetch-holds-head")
def test_actions_marks_a_request_github_never_answered_or_answered_with_a_server_error_unanswered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = Mock(side_effect=[answer(500), answer(599), answer(499), requests.ReadTimeout("read timed out")])
    monkeypatch.setattr(requests, "get", get)
    actions = Actions("example/repo", Token())
    with pytest.raises(FetchUnanswered, match="^GET actions/artifacts/9/zip answered 500$"):
        actions.archive(zipped(9))
    with pytest.raises(FetchUnanswered, match=f"^GET actions/workflows/test.yml/runs\\?.*={COMMIT} answered 599$"):
        actions.json(COMMIT_RUNS_PATH)
    with pytest.raises(FetchRefused, match="^GET actions/artifacts/9/zip answered 499$") as client_error:
        actions.archive(zipped(9))
    assert not isinstance(client_error.value, FetchUnanswered)
    with pytest.raises(FetchUnanswered, match="^GET actions/artifacts/9/zip failed: read timed out$"):
        actions.archive(zipped(9))
    with pytest.raises(FetchUnanswered, match="^GET actions/artifacts/9/zip failed: mint reset$"):
        Actions("example/repo", Unminted()).archive(zipped(9))
    with pytest.raises(FetchRefused, match="^actions/artifacts/5/zip holds no readable packed.json$") as unreadable:
        manifests.fetch(github([1], {1: [artifact(5)]}, {5: b"not a zip"}), COMMIT)
    assert not isinstance(unreadable.value, FetchUnanswered)


@pytest.mark.feature("lab-token-mint-status")
@pytest.mark.parametrize(
    ("status", "refusal"), [(403, FetchRefused), (499, FetchRefused), (500, FetchUnanswered), (599, FetchUnanswered)]
)
def test_actions_classifies_the_real_token_mint_refusal_by_its_http_status(
    monkeypatch: pytest.MonkeyPatch, status: int, refusal: type[FetchRefused]
) -> None:
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "fixture")
    monkeypatch.setattr(github_app.jwt, "encode", Mock(return_value="signed"))
    mint = Mock(return_value=answer(status))
    monkeypatch.setattr(requests, "post", mint)
    get = Mock()
    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(refusal) as refused:
        Actions("example/repo").archive(zipped(9))
    assert type(refused.value) is refusal
    assert str(refused.value) == (
        "GET actions/artifacts/9/zip token mint failed: GitHub App cannot supply scoped actions read credentials: "
        f"GitHub answered {status}"
    )
    assert isinstance(refused.value.__cause__, github_app.GitAuthRefused)
    assert refused.value.__cause__.status_code == status
    get.assert_not_called()
    assert mint.call_count == 1


@pytest.mark.feature("lab-token-mint-status")
def test_actions_keeps_a_mint_scope_refusal_permanent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_APP_ID", "123")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "fixture")
    monkeypatch.setattr(github_app.jwt, "encode", Mock(return_value="signed"))
    mint = Mock(
        return_value=answer(201, {"repositories": [{"full_name": "example/repo"}], "permissions": {"actions": "write"}})
    )
    monkeypatch.setattr(requests, "post", mint)
    get = Mock()
    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(FetchRefused) as refused:
        Actions("example/repo").json(COMMIT_RUNS_PATH)
    assert type(refused.value) is FetchRefused
    assert str(refused.value) == (
        f"GET {COMMIT_RUNS_PATH} token mint failed: GitHub App cannot supply scoped actions read credentials: "
        "GitHub answered 201 with a token outside that scope"
    )
    assert isinstance(refused.value.__cause__, github_app.GitAuthRefused)
    assert refused.value.__cause__.status_code is None
    get.assert_not_called()


@pytest.mark.feature("lab-dev-manifest-fetch")
def test_the_fetcher_reads_each_repository_through_one_reader_of_its_own() -> None:
    made: list[str] = []

    def reader(slug: str) -> FakeGitHub:
        made.append(slug)
        return github([], {}, {})

    fetcher = Fetcher(reader)
    assert fetcher("example/repo", COMMIT) is None
    assert fetcher("example/repo", COMMIT) is None
    assert fetcher("other/repo", COMMIT) is None
    assert made == ["example/repo", "other/repo"]
    assert Fetcher().reader is Actions


WAIT_S = 10.0


@dataclass
class Clock:
    """A clock the test sets."""

    now: float = 0.0

    def __call__(self) -> float:
        """Return the set time."""
        return self.now


@dataclass
class Held:
    """A manifest fetch that records its start, then answers its commit's canned answer once released."""

    answers: dict[str, bytes | None | FetchRefused]
    release: threading.Event = field(default_factory=threading.Event)
    started: threading.Semaphore = field(default_factory=lambda: threading.Semaphore(0))
    asked: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def __call__(self, slug: str, commit: str) -> bytes | None:
        """Record the fetch, wait for its release and answer it."""
        self.asked.append((slug, commit))
        self.started.release()
        self.release.wait(WAIT_S)
        answer = self.answers[commit]
        if isinstance(answer, FetchRefused):
            raise answer
        return answer


def finished(fetches: Mapping[tuple[str, str], tuple[float, Future[bytes | None]]]) -> None:
    """Wait until every fetch a background holds has finished."""
    wait([future for _, future in fetches.values()])


@pytest.mark.feature("lab-manifest-fetch-off-pass")
def test_background_raises_pending_while_a_fetch_runs_and_answers_it_until_it_goes_stale() -> None:
    held = Held({COMMIT: packed(COMMIT), OTHER: None})
    clock = Clock()
    background = manifests.Background(held, clock=clock)
    for _ in range(2):
        with pytest.raises(manifests.FetchPending, match=f"^example/repo manifest of {COMMIT} is still fetching$"):
            background("example/repo", COMMIT)
    clock.now = 1.0
    with pytest.raises(manifests.FetchPending, match=f"^example/repo manifest of {OTHER} is still fetching$"):
        background("example/repo", OTHER)
    held.release.set()
    finished(background.fetches)
    clock.now = 299.0
    assert background("example/repo", COMMIT) == packed(COMMIT)
    assert background("example/repo", COMMIT) == packed(COMMIT)
    assert background("example/repo", OTHER) is None
    assert sorted(held.asked) == [("example/repo", COMMIT), ("example/repo", OTHER)]
    clock.now = 300.0
    assert background("example/repo", OTHER) is None
    assert list(background.fetches) == [("example/repo", OTHER)]
    held.release.clear()
    with pytest.raises(manifests.FetchPending):
        background("example/repo", COMMIT)
    assert all(held.started.acquire(timeout=WAIT_S) for _ in range(3))
    assert sorted(held.asked) == [("example/repo", COMMIT), ("example/repo", COMMIT), ("example/repo", OTHER)]
    held.release.set()


@pytest.mark.feature("lab-manifest-fetch-off-pass")
def test_background_answers_a_failed_fetch_once_and_fetches_it_again_on_the_next_call() -> None:
    held = Held({COMMIT: FetchUnanswered("GET runs answered 502")})
    background = manifests.Background(held)
    with pytest.raises(manifests.FetchPending) as pending:
        background("example/repo", COMMIT)
    assert isinstance(pending.value, FetchUnanswered)
    held.release.set()
    finished(background.fetches)
    with pytest.raises(FetchUnanswered, match="^GET runs answered 502$"):
        background("example/repo", COMMIT)
    held.release.clear()
    with pytest.raises(manifests.FetchPending):
        background("example/repo", COMMIT)
    assert all(held.started.acquire(timeout=WAIT_S) for _ in range(2))
    assert held.asked == [("example/repo", COMMIT), ("example/repo", COMMIT)]
    held.release.set()


@pytest.mark.feature("lab-manifest-fetch-off-pass")
def test_background_forgets_a_failed_fetch_nobody_asked_again_once_its_fresh_window_ends() -> None:
    held = Held({COMMIT: FetchUnanswered("GET runs answered 502"), OTHER: None})
    clock = Clock()
    background = manifests.Background(held, clock=clock)
    with pytest.raises(manifests.FetchPending):
        background("example/repo", COMMIT)
    held.release.set()
    finished(background.fetches)
    held.release.clear()
    clock.now = 299.0
    with pytest.raises(manifests.FetchPending):
        background("example/repo", OTHER)
    assert sorted(background.fetches) == [("example/repo", COMMIT), ("example/repo", OTHER)]
    clock.now = 300.0
    with pytest.raises(manifests.FetchPending):
        background("example/repo", OTHER)
    assert list(background.fetches) == [("example/repo", OTHER)]
    held.release.set()


@pytest.mark.feature("lab-manifest-fetch-off-pass")
def test_background_runs_at_most_four_fetches_at_once() -> None:
    commits = [f"{number:040x}" for number in range(5)]
    held = Held({commit: None for commit in commits})
    background = manifests.Background(held)
    for commit in commits:
        with pytest.raises(manifests.FetchPending):
            background("example/repo", commit)
    assert all(held.started.acquire(timeout=WAIT_S) for _ in range(4))
    assert not held.started.acquire(timeout=0.2)
    held.release.set()
    assert held.started.acquire(timeout=WAIT_S)
    finished(background.fetches)
    assert [background("example/repo", commit) for commit in commits] == [None] * 5
    assert sorted(held.asked) == [("example/repo", commit) for commit in commits]
