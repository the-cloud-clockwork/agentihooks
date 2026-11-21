import re
import subprocess

import pytest

from scripts.forge import gitlab, remote
from scripts.forge.github import GitHubForge
from scripts.forge.gitlab import GitLabForge
from scripts.forge.model import ForgeError
from tests.forge.fakes import API, FEATURE, Ran

pytestmark = pytest.mark.unit


@pytest.mark.feature(FEATURE)
@pytest.mark.parametrize(
    ("url", "host", "path"),
    [
        ("https://github.com/Org/repo.git", "github.com", "Org/repo"),
        ("  https://github.com/Org/repo\n", "github.com", "Org/repo"),
        ("git@github.com:Org/repo.git", "github.com", "Org/repo"),
        ("ssh://git@gitlab.example.com:2222/group/sub/repo.git", "gitlab.example.com", "group/sub/repo"),
        ("https://oauth2:x@GitLab.com/group/repo/", "gitlab.com", "group/repo"),
    ],
)
def test_a_remote_url_parses_into_host_and_repository_path(url: str, host: str, path: str) -> None:
    assert remote.parse(url) == remote.Remote(host, path)


@pytest.mark.feature(FEATURE)
@pytest.mark.parametrize("url", ["/home/user/repo", "https://github.com/repo", ""])
def test_a_url_without_host_and_owner_is_refused(url: str) -> None:
    with pytest.raises(ValueError, match=f"^not a repository remote: {re.escape(url)}$"):
        remote.parse(url)


@pytest.mark.feature(FEATURE)
@pytest.mark.parametrize(
    ("host", "kind"),
    [
        ("github.com", "github"),
        ("gitlab.com", "gitlab"),
        ("gitlab.example.org", "gitlab"),
        ("code.gitlab.corp", "gitlab"),
    ],
)
def test_the_picker_names_the_forge_from_the_remote_host(host: str, kind: str) -> None:
    assert remote.kind_of(host) == kind


@pytest.mark.feature(FEATURE)
@pytest.mark.parametrize("host", ["github.example.com", "notgitlab.com", "bitbucket.org"])
def test_a_host_that_names_no_forge_is_refused(host: str) -> None:
    expected = f"no forge known for host {host}; pass --forge github or gitlab"
    with pytest.raises(ValueError, match=f"^{re.escape(expected)}$"):
        remote.kind_of(host)


@pytest.mark.feature(FEATURE)
def test_the_picker_returns_github_for_a_github_remote() -> None:
    chosen = remote.pick(remote.Remote("github.com", "Org/repo"), None, {})
    assert isinstance(chosen, GitHubForge)
    assert (chosen.repo, chosen.run) == ("github.com/Org/repo", subprocess.run)


@pytest.mark.feature(FEATURE)
def test_the_picker_returns_gitlab_with_the_token_from_the_environment() -> None:
    chosen = remote.pick(remote.Remote("gitlab.example.com", "group/sub/repo"), None, {"GITLAB_TOKEN": "fake-token"})
    assert isinstance(chosen, GitLabForge)
    assert (chosen.api, chosen.web, chosen.token, chosen.send) == (
        API,
        "https://gitlab.example.com/group/sub/repo",
        "fake-token",
        gitlab.urlopen_transport,
    )


@pytest.mark.feature(FEATURE)
def test_an_explicit_kind_overrides_the_remote_host() -> None:
    where = remote.Remote("git.example.com", "group/repo")
    assert isinstance(remote.pick(where, "gitlab", {"GITLAB_TOKEN": "fake-token"}), GitLabForge)
    chosen = remote.pick(where, "github", {})
    assert isinstance(chosen, GitHubForge)
    assert chosen.repo == "git.example.com/group/repo"


@pytest.mark.feature(FEATURE)
@pytest.mark.parametrize("env", [{}, {"GITLAB_TOKEN": ""}])
def test_gitlab_without_a_token_is_refused(env: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="^GITLAB_TOKEN is not set$"):
        remote.pick(remote.Remote("gitlab.com", "group/repo"), None, env)


@pytest.mark.feature(FEATURE)
def test_the_origin_remote_is_read_from_git() -> None:
    ran = Ran((0, "git@github.com:Org/repo.git\n"))
    assert remote.origin("/work/repo", ran) == "git@github.com:Org/repo.git"
    assert ran.calls == [
        (
            ["git", "-C", "/work/repo", "remote", "get-url", "origin"],
            {"capture_output": True, "text": True, "timeout": 30},
        )
    ]


@pytest.mark.feature(FEATURE)
def test_a_checkout_without_an_origin_remote_is_refused() -> None:
    with pytest.raises(ForgeError, match="^no origin remote in /work/repo$"):
        remote.origin("/work/repo", Ran((2, "")))
