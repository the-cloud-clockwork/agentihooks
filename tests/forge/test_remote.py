import re
import subprocess

import pytest

from scripts.forge import gitlab, remote
from scripts.forge.github import GitHubForge
from scripts.forge.gitlab import GitLabForge
from scripts.forge.model import ForgeError
from tests.forge.fakes import API, Ran

pytestmark = pytest.mark.unit


@pytest.mark.feature("forge-change-request-seam")
@pytest.mark.parametrize(
    ("url", "host", "path"),
    [
        ("https://github.com/Org/repo.git", "github.com", "Org/repo"),
        ("  https://github.com/Org/repo\n", "github.com", "Org/repo"),
        ("git@github.com:Org/repo.git", "github.com", "Org/repo"),
        ("ssh://git@gitlab.example.com:2222/group/sub/repo.git", "gitlab.example.com", "group/sub/repo"),
        ("https://oauth2:x@GitLab.com/group/repo/", "gitlab.com", "group/repo"),
        ("https://github.com/Xavier/repoX/", "github.com", "Xavier/repoX"),
    ],
)
def test_a_remote_url_parses_into_host_and_repository_path(url: str, host: str, path: str) -> None:
    assert remote.parse(url) == remote.Remote(host, path)


@pytest.mark.feature("forge-change-request-seam")
@pytest.mark.parametrize(
    ("url", "named"),
    [
        ("/home/user/repo", "/home/user/repo"),
        ("https://github.com/repo", "github.com/repo"),
        ("", "/"),
        ("https://oauth2:hidden@gitlab.com/group", "gitlab.com/group"),
    ],
)
def test_a_url_without_host_and_owner_is_refused_without_its_credentials(url: str, named: str) -> None:
    with pytest.raises(ValueError, match=f"^not a repository remote: {re.escape(named)}$"):
        remote.parse(url)


@pytest.mark.feature("forge-change-request-seam")
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


@pytest.mark.feature("forge-change-request-seam")
@pytest.mark.parametrize("host", ["github.example.com", "notgitlab.com", "bitbucket.org"])
def test_a_host_that_names_no_forge_is_refused(host: str) -> None:
    expected = f"no forge known for host {host}; pass --forge github or gitlab"
    with pytest.raises(ValueError, match=f"^{re.escape(expected)}$"):
        remote.kind_of(host)


@pytest.mark.feature("forge-change-request-seam")
def test_the_picker_returns_github_for_a_github_remote() -> None:
    chosen = remote.pick(remote.Remote("github.com", "Org/repo"), None, {})
    assert isinstance(chosen, GitHubForge)
    assert (chosen.repo, chosen.run) == ("github.com/Org/repo", subprocess.run)


@pytest.mark.feature("forge-change-request-seam")
def test_the_picker_returns_gitlab_with_the_token_from_the_environment() -> None:
    chosen = remote.pick(remote.Remote("gitlab.example.com", "group/sub/repo"), None, {"GITLAB_TOKEN": "fake-token"})
    assert isinstance(chosen, GitLabForge)
    assert (chosen.api, chosen.web, chosen.token, chosen.send) == (
        API,
        "https://gitlab.example.com/group/sub/repo",
        "fake-token",
        gitlab.urlopen_transport,
    )


@pytest.mark.feature("forge-change-request-seam")
def test_an_explicit_kind_overrides_the_remote_host() -> None:
    where = remote.Remote("git.example.com", "group/repo")
    assert isinstance(remote.pick(where, "gitlab", {"GITLAB_TOKEN": "fake-token"}), GitLabForge)
    chosen = remote.pick(where, "github", {})
    assert isinstance(chosen, GitHubForge)
    assert chosen.repo == "git.example.com/group/repo"


@pytest.mark.feature("forge-change-request-seam")
@pytest.mark.parametrize("env", [{}, {"GITLAB_TOKEN": ""}])
def test_gitlab_without_a_token_is_refused(env: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="^GITLAB_TOKEN is not set$"):
        remote.pick(remote.Remote("gitlab.com", "group/repo"), None, env)


@pytest.mark.feature("forge-change-request-seam")
def test_the_origin_remote_is_read_from_git() -> None:
    ran = Ran((0, "git@github.com:Org/repo.git\n"))
    assert remote.origin("/work/repo", ran) == "git@github.com:Org/repo.git"
    assert ran.calls == [
        (
            ["git", "-C", "/work/repo", "remote", "get-url", "origin"],
            {"capture_output": True, "text": True, "timeout": 30},
        )
    ]


@pytest.mark.feature("forge-change-request-seam")
def test_a_checkout_without_an_origin_remote_is_refused() -> None:
    with pytest.raises(ForgeError, match="^no origin remote in /work/repo$"):
        remote.origin("/work/repo", Ran((2, "")))


GITHUB_LINKS = [
    (
        "https://github.com/the-cloud-clockwork/agentihooks/pull/13491",
        ("github", "github.com", "the-cloud-clockwork/agentihooks", 13491),
    ),
    ("https://github.com/Org/.github/pull/7", ("github", "github.com", "Org/.github", 7)),
    ("https://github.com/Org/pull/pull/10", ("github", "github.com", "Org/pull", 10)),
]
GITLAB_LINKS = [
    ("https://gitlab.com/group/sub/project/-/merge_requests/7", ("gitlab", "gitlab.com", "group/sub/project", 7)),
    ("https://gitlab.example.com/team/app/-/merge_requests/42", ("gitlab", "gitlab.example.com", "team/app", 42)),
    ("https://code.gitlab.corp/a/b.c/d_e/f-g/-/merge_requests/1", ("gitlab", "code.gitlab.corp", "a/b.c/d_e/f-g", 1)),
    ("https://gitlab.com/my.group/pull/-/merge_requests/90", ("gitlab", "gitlab.com", "my.group/pull", 90)),
]
REFUSED_GITHUB_LINKS = [
    "https://github.com/Org/repo/issues/12",
    "http://github.com/Org/repo/pull/12",
    "https://github.com/Org/repo/pull/12/",
    "https://github.com/Org/repo/pull/12/files",
    "https://github.com/Org/repo/pull/12?w=1",
    "https://github.com/Org/repo/pull/12#discussion_r1",
    "https://github.com/Org/repo/pull/0",
    "https://github.com/Org/repo/pull/012",
    "https://github.com/Org/repo/pull/",
    "https://github.com/Org/repo/pull/twelve",
    "https://github.com/Org/pull/12",
    "https://github.com/Org/team/repo/pull/12",
    "https://www.github.com/Org/repo/pull/12",
    "https://GitHub.com/Org/repo/pull/12",
    "https://github.example.com/Org/repo/pull/12",
    "https://user:secret@github.com/Org/repo/pull/12",
    "https://github.com/Org/repo/-/merge_requests/12",
    " https://github.com/Org/repo/pull/12",
    "https://github.com/Org/repo/pull/12\n",
    "github.com/Org/repo/pull/12",
    "",
]
REFUSED_GITLAB_LINKS = [
    "https://gitlab.com/group/project/-/issues/3",
    "http://gitlab.com/group/project/-/merge_requests/3",
    "https://gitlab.com/group/project/-/merge_requests/3/",
    "https://gitlab.com/group/project/-/merge_requests/3#note_5",
    "https://gitlab.com/group/project/-/merge_requests/3?tab=diffs",
    "https://gitlab.com/group/project/-/merge_requests/3/diffs",
    "https://gitlab.com/group/project/-/merge_requests/0",
    "https://gitlab.com/group/project/merge_requests/3",
    "https://gitlab.com/group/project/pull/3",
    "https://gitlab.com/project/-/merge_requests/3",
    "https://gitlab.com/group/-/project/-/merge_requests/3",
    "https://code.example.com/group/project/-/merge_requests/3",
    "https://notgitlab.com/group/project/-/merge_requests/3",
    "https://GitLab.com/group/project/-/merge_requests/3",
    "https://gitlab.example.com:8443/group/project/-/merge_requests/3",
]


@pytest.mark.feature("forge-change-link-parse")
def test_today_the_remote_parser_reads_a_project_web_link_and_its_clone_remote() -> None:
    github = remote.Remote("github.com", "the-cloud-clockwork/agentihooks")
    assert remote.parse("https://github.com/the-cloud-clockwork/agentihooks") == github
    assert remote.parse("https://github.com/the-cloud-clockwork/agentihooks.git") == github
    gitlab_project = remote.Remote("gitlab.example.com", "team/platform/app")
    assert remote.parse("https://gitlab.example.com/team/platform/app.git") == gitlab_project
    assert (remote.kind_of("github.com"), remote.kind_of("gitlab.example.com")) == ("github", "gitlab")


@pytest.mark.feature("forge-change-link-parse")
@pytest.mark.parametrize(("link", "fields"), GITHUB_LINKS)
def test_a_github_pull_link_parses_into_its_forge_project_and_number(
    link: str, fields: tuple[str, str, str, int]
) -> None:
    kind, host, path, number = fields
    parsed = remote.change_link(link)
    web = f"https://{host}/{path}"
    assert parsed == remote.ChangeLink(kind, host, path, number, web, f"{web}.git")
    with pytest.raises(AttributeError):
        setattr(parsed, "number", 1)


@pytest.mark.feature("forge-change-link-parse")
def test_a_github_pull_link_names_its_project_web_link_and_clone_remote() -> None:
    parsed = remote.change_link("https://github.com/the-cloud-clockwork/agentihooks/pull/13491")
    assert (parsed.web, parsed.clone) == (
        "https://github.com/the-cloud-clockwork/agentihooks",
        "https://github.com/the-cloud-clockwork/agentihooks.git",
    )


@pytest.mark.feature("forge-change-link-parse")
@pytest.mark.parametrize("link", REFUSED_GITHUB_LINKS)
def test_a_malformed_or_foreign_github_link_is_refused(link: str) -> None:
    with pytest.raises(ValueError) as refused:
        remote.change_link(link)
    assert str(refused.value) == "not a GitHub pull link or GitLab merge request link"


@pytest.mark.feature("forge-change-link-parse")
@pytest.mark.parametrize(("link", "fields"), GITLAB_LINKS)
def test_a_gitlab_merge_request_link_keeps_its_nested_groups_whole(
    link: str, fields: tuple[str, str, str, int]
) -> None:
    kind, host, path, number = fields
    web = f"https://{host}/{path}"
    assert remote.change_link(link) == remote.ChangeLink(kind, host, path, number, web, f"{web}.git")


@pytest.mark.feature("forge-change-link-parse")
@pytest.mark.parametrize("link", REFUSED_GITLAB_LINKS)
def test_a_malformed_or_unlabelled_gitlab_link_is_refused(link: str) -> None:
    with pytest.raises(ValueError) as refused:
        remote.change_link(link)
    assert str(refused.value) == "not a GitHub pull link or GitLab merge request link"


@pytest.mark.feature("forge-change-link-parse")
@pytest.mark.parametrize("link", [link for link, _ in GITHUB_LINKS + GITLAB_LINKS])
def test_the_web_link_and_clone_remote_read_back_to_the_same_project(link: str) -> None:
    parsed = remote.change_link(link)
    project = remote.Remote(parsed.host, parsed.path)
    assert (remote.parse(parsed.web), remote.parse(parsed.clone)) == (project, project)
    assert parsed.kind == remote.kind_of(parsed.host)


@pytest.mark.feature("forge-change-link-parse")
@pytest.mark.parametrize(
    ("link", "accepted"),
    [(link, True) for link, _ in GITHUB_LINKS + GITLAB_LINKS]
    + [(link, False) for link in REFUSED_GITHUB_LINKS + REFUSED_GITLAB_LINKS],
)
def test_the_shared_pattern_accepts_exactly_the_links_the_parser_accepts(link: str, accepted: bool) -> None:
    assert (remote.CHANGE_LINK.fullmatch(link) is not None) is accepted


@pytest.mark.feature("forge-change-link-parse")
def test_the_shared_pattern_finds_change_links_in_text_as_plain_strings() -> None:
    text = "Merged https://github.com/Org/repo/pull/12, then https://gitlab.com/group/sub/app/-/merge_requests/3."
    assert remote.CHANGE_LINK.findall(text) == [
        "https://github.com/Org/repo/pull/12",
        "https://gitlab.com/group/sub/app/-/merge_requests/3",
    ]
