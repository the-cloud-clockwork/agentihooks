import base64
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest

from scripts.ci_lab import contract, git_auth, relay, scheduler
from scripts.ci_lab.digests import Toolchain
from scripts.ci_lab.intake import HeadEvent
from scripts.ci_lab.objects import LocalObjects
from scripts.ci_lab.worktrees import Mirror
from scripts.github_app import GitAuthRefused
from tests.ci_lab.test_scheduler import Run
from tests.ci_lab.test_scheduler import lab as lab
from tests.ci_lab.test_scheduler import origin as origin
from tests.ci_lab.test_scheduler_retire import REPO, _listing, _queued, unit
from tests.ci_lab.test_scheduler_uncarried import ACTOR, FakeKube, Inbox, Poster, _git, _no_tests, _unknown, fake_redis

PRIVATE = "https://github.com/the-cloud-clockwork/antoncore"
OTHER = "https://github.com/the-cloud-clockwork/agentihooks-bundle"
TOKEN = "fixture"
HEADER = "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()


class Token:
    def __init__(self, value: str = TOKEN) -> None:
        self.value = value
        self.calls = 0

    def get_token(self) -> str:
        self.calls += 1
        return self.value


class PrivateGit:
    def __init__(self, repo: Path, run: Callable[..., Any]) -> None:
        self.repo = repo
        self.run = run
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        command = list(argv)
        if PRIVATE in command or OTHER in command:
            environ = dict(kwargs.get("env") or os.environ)
            self.calls.append(command)
            headers = {
                environ.get(f"GIT_CONFIG_KEY_{index}"): environ.get(f"GIT_CONFIG_VALUE_{index}")
                for index in range(int(environ.get("GIT_CONFIG_COUNT", "0")))
            }
            if headers.get("http.https://github.com/.extraheader") != HEADER:
                reason = "fatal: could not read Username for 'https://github.com': terminal prompts disabled"
                if kwargs.get("check"):
                    raise subprocess.CalledProcessError(128, command, "", reason)
                return subprocess.CompletedProcess(command, 128, "", reason)
            command = [f"file://{self.repo}" if value in (PRIVATE, OTHER) else value for value in command]
        return cast(subprocess.CompletedProcess[str], self.run(command, **kwargs))


@pytest.fixture
def private_git(origin: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch) -> PrivateGit:
    repo, base, head = origin
    _git(repo, "update-ref", "refs/pull/42/head", head)
    _git(repo, "update-ref", "refs/heads/gh-readonly-queue/dev/pr-42", head)
    _git(repo, "update-ref", "refs/heads/dev", base)
    protected = PrivateGit(repo, subprocess.run)
    monkeypatch.setattr(subprocess, "run", protected)
    return protected


def expander(tmp_path: Path, token: Token) -> scheduler.MirrorExpander:
    return scheduler.MirrorExpander(
        tmp_path / "node",
        tmp_path / "stores",
        Toolchain("3.12", {}, "", "", "image", {}),
        _no_tests,
        _unknown,
        auth=lambda _repo: token,
    )


@pytest.mark.feature("lab-private-repository-refs")
def test_an_anonymous_private_listing_reproduces_the_credential_refusal(private_git: PrivateGit) -> None:
    with pytest.raises(subprocess.CalledProcessError) as failed:
        subprocess.run(["git", "ls-remote", PRIVATE, "refs/heads/dev"], check=True, capture_output=True, text=True)
    assert failed.value.returncode == 128
    assert "could not read Username" in failed.value.stderr


@pytest.mark.feature("lab-private-repository-refs")
def test_private_pull_heads_are_reachable_with_a_read_token(
    tmp_path: Path,
    origin: tuple[Path, str, str],
    private_git: PrivateGit,
) -> None:
    token = Token()
    assert expander(tmp_path, token).reachable(PRIVATE, origin[2]) is True
    assert token.calls == 1
    assert len(private_git.calls) == 1
    assert TOKEN not in str(private_git.calls[0])


@pytest.mark.feature("lab-private-repository-refs")
def test_private_merge_groups_are_listed_with_a_read_token(
    tmp_path: Path,
    origin: tuple[Path, str, str],
    private_git: PrivateGit,
) -> None:
    assert expander(tmp_path, Token()).groups(PRIVATE) == frozenset({origin[2]})
    assert len(private_git.calls) == 1


@pytest.mark.feature("lab-private-repository-refs")
def test_private_merge_base_lookup_authenticates_dev_listing_and_both_fetches(
    tmp_path: Path,
    origin: tuple[Path, str, str],
    private_git: PrivateGit,
) -> None:
    assert expander(tmp_path, Token()).merge_base(PRIVATE, origin[2]) == origin[1]
    assert sum("fetch" in argv for argv in private_git.calls) == 2
    assert all(TOKEN not in str(argv) for argv in private_git.calls)


@pytest.mark.feature("lab-private-repository-refs")
def test_a_private_mirror_fetch_uses_its_environment_and_then_reuses_the_commit(
    tmp_path: Path,
    origin: tuple[Path, str, str],
    private_git: PrivateGit,
) -> None:
    mirror = Mirror(tmp_path / "node", tmp_path / "store", PRIVATE, environ=git_auth.environ(Token()))
    assert mirror.fetch(origin[2]) is True
    assert mirror.fetch(origin[2]) is False
    assert len(private_git.calls) == 1
    result = subprocess.run(
        ["git", "-C", str(mirror.store), "config", "--local", "--get-regexp", "http"], capture_output=True, text=True
    )
    assert result.stdout == ""


@pytest.mark.feature("lab-private-repository-refs")
def test_a_mirror_representation_never_contains_its_credential_environment(tmp_path: Path) -> None:
    mirror = Mirror(tmp_path / "node", tmp_path / "store", PRIVATE, environ={"credential": TOKEN})
    assert TOKEN not in repr(mirror)


@pytest.mark.feature("lab-private-repository-refs")
def test_the_shared_git_environment_matches_the_relay_header_and_disables_prompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAB_PRIVATE_REFS_CONTROL", "preserved")
    token = Token()
    environ = git_auth.environ(token)
    assert environ["GIT_CONFIG_COUNT"] == "1"
    assert environ["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert environ["GIT_CONFIG_VALUE_0"] == HEADER
    assert environ["GIT_TERMINAL_PROMPT"] == "0"
    assert environ["LAB_PRIVATE_REFS_CONTROL"] == "preserved"
    assert token.calls == 1


@pytest.mark.feature("lab-private-repository-refs")
def test_relay_push_uses_the_shared_environment_builder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    environment = Mock(return_value={"shared": "credential"})
    monkeypatch.setattr(git_auth, "environ", environment)
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(subprocess, "run", run)
    token = Token()
    mirror = Mock(spec=Mirror, store=tmp_path / "store")
    relay.GitPusher(mirror, "the-cloud-clockwork/antoncore", token).push("a" * 40, "branch")
    environment.assert_called_once_with(token)
    assert run.call_args.kwargs["env"] == {"shared": "credential"}


@pytest.mark.feature("lab-private-repository-refs")
def test_a_private_pull_starts_grading_without_an_expansion_refusal(
    tmp_path: Path,
    origin: tuple[Path, str, str],
    private_git: PrivateGit,
) -> None:
    inbox, poster = Inbox(), Poster()
    ports = scheduler.Ports(inbox, expander(tmp_path, Token()), lambda _repo: poster, frozenset[str], FakeKube())
    engine = scheduler.Scheduler(
        fake_redis(),
        LocalObjects(tmp_path / "objects"),
        ports,
        scheduler.Settings(frozenset({ACTOR}), 0),
    )
    inbox.pending.append(
        scheduler.Head(HeadEvent("pull_request", PRIVATE, origin[2], origin[1], ACTOR, "synchronize"), "pr-42")
    )
    actions = engine.tick(1000)
    assert any(action.startswith("grading ") for action in actions)
    assert not any("cannot expand" in action for action in actions)
    assert not any(post[4].title == "grade cannot expand" for post in poster.posts)
    assert private_git.calls


@pytest.mark.feature("lab-private-repository-refs")
def test_a_token_refusal_is_visible_to_the_waiting_pass_without_logging_the_token(
    tmp_path: Path,
    origin: tuple[Path, str, str],
    private_git: PrivateGit,
) -> None:
    refused = Mock(side_effect=GitAuthRefused("repository contents read denied"))
    reader = expander(tmp_path, Token())
    reader.auth = refused
    inbox, poster = Inbox(), Poster()
    ports = scheduler.Ports(inbox, reader, lambda _repo: poster, frozenset[str], FakeKube())
    engine = scheduler.Scheduler(
        fake_redis(),
        LocalObjects(tmp_path / "objects"),
        ports,
        scheduler.Settings(frozenset({ACTOR}), 0),
    )
    inbox.pending.append(
        scheduler.Head(HeadEvent("pull_request", PRIVATE, origin[2], origin[1], ACTOR, "synchronize"), "pr-42")
    )
    actions = engine.tick(1000)
    assert any("cannot expand" in action and "repository contents read denied" in action for action in actions)
    assert TOKEN not in str(actions)
    assert any(post[4].title == "grade cannot expand" for post in poster.posts)
    assert private_git.calls == []


@pytest.mark.feature("lab-private-repository-refs")
def test_file_origins_need_no_app_credentials(tmp_path: Path, origin: tuple[Path, str, str]) -> None:
    token = Token()
    reader = expander(tmp_path, token)
    assert reader.reachable(f"file://{origin[0]}", origin[2]) is True
    assert token.calls == 0


@pytest.mark.feature("lab-private-repository-refs")
def test_production_reuses_one_scoped_contents_read_auth_per_repository(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    origin: tuple[Path, str, str],
    private_git: PrivateGit,
) -> None:
    auth = Mock(side_effect=lambda *_args, **_kwargs: Token())
    monkeypatch.setattr(scheduler, "GitHubAppAuth", auth, raising=False)
    monkeypatch.setattr(scheduler, "worker_pods", lambda _environ: FakeKube())
    environ = {
        contract.GIT_MIRROR: str(tmp_path / "node"),
        scheduler.MIRROR_STORE: str(tmp_path / "stores"),
        scheduler.REPOSITORY: PRIVATE,
    }
    engine = scheduler.build(environ, fake_redis(), LocalObjects(tmp_path / "objects"), Mock(), Mock())
    assert engine.ports.expander.reachable(PRIVATE, origin[2]) is True
    assert engine.ports.expander.groups(OTHER) == frozenset({origin[2]})
    assert auth.call_count == 2
    assert auth.call_args_list[0].args == ("github.com/the-cloud-clockwork/antoncore",)
    assert auth.call_args_list[1].args == ("github.com/the-cloud-clockwork/agentihooks-bundle",)
    assert all(call.kwargs == {"permissions": {"contents": "read"}} for call in auth.call_args_list)


@pytest.mark.feature("lab-private-repository-refs")
def test_a_read_token_refusal_preserves_live_merge_groups(
    lab: Callable[..., Run], monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _queued(lab, unit(1))
    live = {grade.run for grade in run.grades()}
    pending = run.engine.queue.pending()
    error = GitAuthRefused("repository contents read denied")
    monkeypatch.setattr(_listing(run), "groups", Mock(side_effect=error))
    assert run.tick(1100) == [f"cannot list the merge groups of {REPO}: {error}"]
    assert run.lab.client.smembers(scheduler.LIVE) == live
    assert run.engine.queue.pending() == pending
