import json

import pytest

from scripts.forge import github
from scripts.forge.model import ChangeRequest, Check, ForgeError
from tests.forge.fakes import Ran, fixture

pytestmark = pytest.mark.unit
REPO = "github.com/o/r"
CALLED = {"capture_output": True, "text": True, "timeout": 60}


@pytest.mark.feature("forge-change-request-seam")
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
        files=("scripts/example.py", "tests/test_example.py"),
        checks=(Check("lint", "SUCCESS"), Check("unit", "IN_PROGRESS"), Check("sonar", "FAILURE")),
    )
    fields = "number,url,title,body,headRefOid,files,statusCheckRollup"
    assert ran.calls == [
        (["gh", "pr", "view", "7", "--json", fields, "--repo", REPO], {"input": None, **CALLED}),
        (["gh", "pr", "diff", "7", "--repo", REPO], {"input": None, **CALLED}),
    ]


@pytest.mark.feature("forge-change-request-seam")
def test_github_posts_a_review_comment_with_the_body_on_standard_input() -> None:
    ran = Ran((0, "https://github.com/o/r/pull/7#issuecomment-1\n"))
    assert github.GitHubForge(REPO, ran).post(7, "Finding one") == "https://github.com/o/r/pull/7#issuecomment-1"
    assert ran.calls == [
        (["gh", "pr", "comment", "7", "--body-file", "-", "--repo", REPO], {"input": "Finding one", **CALLED})
    ]


@pytest.mark.feature("forge-change-request-seam")
def test_a_failed_gh_call_raises_with_its_error_text() -> None:
    with pytest.raises(ForgeError, match="^gh failed: boom$"):
        github.GitHubForge(REPO, Ran((1, ""))).read(7)


def _pulls(*rows: tuple[int, str, str]) -> str:
    return json.dumps([{"number": number, "base": {"ref": base}, "head": {"ref": head}} for number, base, head in rows])


@pytest.mark.feature("forge-change-request-seam")
def test_github_names_the_promotion_pull_request_that_produced_a_release_tag() -> None:
    promotion = (41, "main", f"promotion/{'a' * 40}")
    ran = Ran((0, json.dumps({"sha": "c0ffee"})), (0, _pulls((40, "dev", "engineer-1"), promotion)))
    assert github.GitHubForge(REPO, ran).release_pull("v1.2.3") == 41
    assert ran.calls == [
        (["gh", "api", "--hostname", "github.com", "repos/o/r/commits/v1.2.3"], {"input": None, **CALLED}),
        (["gh", "api", "--hostname", "github.com", "repos/o/r/commits/c0ffee/pulls"], {"input": None, **CALLED}),
    ]


@pytest.mark.feature("forge-change-request-seam")
@pytest.mark.parametrize(
    "rows",
    [
        (),
        ((40, "dev", "promotion/x"),),
        ((40, "main", "engineer-1"),),
        ((40, "main", "promotion/x"), (41, "main", "promotion/y")),
    ],
)
def test_a_release_tag_without_one_promotion_pull_request_is_refused(rows: tuple[tuple[int, str, str], ...]) -> None:
    ran = Ran((0, json.dumps({"sha": "c0ffee"})), (0, _pulls(*rows)))
    with pytest.raises(ForgeError, match="^no single promotion pull request produced tag v1.2.3$"):
        github.GitHubForge(REPO, ran).release_pull("v1.2.3")
