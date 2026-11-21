import pytest

from scripts.forge import github
from scripts.forge.model import ChangedFile, ChangeRequest, Check, ForgeError
from tests.forge.fakes import FEATURE, Ran, fixture

pytestmark = pytest.mark.unit
REPO = "github.com/o/r"
CALLED = {"capture_output": True, "text": True, "timeout": 60}


@pytest.mark.feature(FEATURE)
def test_github_reads_a_recorded_pull_request_into_a_change_request() -> None:
    diff = fixture("github_pr.diff")
    ran = Ran((0, fixture("github_pr.json")), (0, diff))
    change = github.GitHubForge(REPO, ran).read(7)
    assert change == ChangeRequest(
        number=7,
        url="https://github.com/o/r/pull/7",
        title="Add the example",
        body="Reviewer seats read the change through the forge.",
        head="eac251ba7f4ed988ecfa0232d1f91fb5cbe900e2",
        diff=diff,
        files=(ChangedFile("scripts/example.py", 12, 3), ChangedFile("tests/test_example.py", 4, 0)),
        checks=(Check("lint", "SUCCESS"), Check("unit", "IN_PROGRESS"), Check("sonar", "FAILURE")),
    )
    fields = "number,url,title,body,headRefOid,files,statusCheckRollup"
    assert ran.calls == [
        (["gh", "pr", "view", "7", "--json", fields, "--repo", REPO], {"input": None, **CALLED}),
        (["gh", "pr", "diff", "7", "--repo", REPO], {"input": None, **CALLED}),
    ]


@pytest.mark.feature(FEATURE)
def test_github_posts_a_review_comment_with_the_body_on_standard_input() -> None:
    ran = Ran((0, "https://github.com/o/r/pull/7#issuecomment-1\n"))
    assert github.GitHubForge(REPO, ran).post(7, "Finding one") == "https://github.com/o/r/pull/7#issuecomment-1"
    assert ran.calls == [
        (["gh", "pr", "comment", "7", "--body-file", "-", "--repo", REPO], {"input": "Finding one", **CALLED})
    ]


@pytest.mark.feature(FEATURE)
def test_a_failed_gh_call_raises_with_its_error_text() -> None:
    with pytest.raises(ForgeError, match="^gh failed: boom$"):
        github.GitHubForge(REPO, Ran((1, ""))).read(7)
