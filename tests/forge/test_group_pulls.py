"""A forge lists the pull requests a merge queue group tests."""

import pytest

from scripts.forge import github, gitlab
from scripts.forge.model import ForgeError
from tests.forge.fakes import Ran, Sent

pytestmark = pytest.mark.unit
REPO = "github.com/o/r"
CALLED = {"capture_output": True, "text": True, "timeout": 60}
SUBJECTS = '.commits[].commit.message | split("\\n")[0]'


@pytest.mark.feature("review-queue-group-post")
def test_github_lists_the_unique_pull_requests_the_group_commit_titles_end_with() -> None:
    titles = "Add the gate (#12)\nFix a typo (#9)\nRevert the (#3) change (#14)\nNo number\nAgain (#12)\nSpace (#5) \n"
    ran = Ran((0, titles))
    assert github.GitHubForge(REPO, ran).group_pulls("b1", "h1") == (12, 9, 14, 5)
    assert ran.calls == [
        (["gh", "api", "--hostname", "github.com", "repos/o/r/compare/b1...h1", "--jq", SUBJECTS], CALLED)
    ]


@pytest.mark.feature("review-queue-group-post")
def test_github_lists_no_pull_request_when_no_title_ends_with_a_number() -> None:
    ran = Ran((0, "Plain title\nTitle (#x)\n#7 at the start\n"))
    assert github.GitHubForge("example.com/team/app", ran).group_pulls("b1", "h1") == ()
    assert ran.calls[0][0][2:5] == ["--hostname", "example.com", "repos/team/app/compare/b1...h1"]


@pytest.mark.feature("review-queue-group-post")
def test_a_failed_compare_raises_with_its_error_text() -> None:
    with pytest.raises(ForgeError, match="^gh failed: boom$"):
        github.GitHubForge(REPO, Ran((1, ""))).group_pulls("b1", "h1")


@pytest.mark.feature("review-queue-group-post")
def test_gitlab_refuses_merge_queue_groups_without_a_request() -> None:
    sent = Sent()
    forge = gitlab.GitLabForge("gitlab.example.com", "group/project", "token", sent)
    with pytest.raises(ForgeError, match="^GitLab merge requests have no merge queue groups$"):
        forge.group_pulls("b1", "h1")
    assert sent.calls == []
