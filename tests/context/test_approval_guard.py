"""Agent sessions cannot approve a pull request: every approval form is refused, comments and change requests pass."""

import shlex
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from hooks import hook_manager
from hooks.context.approval_guard import refusal
from hooks.hook_manager import BlockAction

REFUSED = r"BLOCKED \[pull request approval\]"


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in ("AGENTIHOOKS_SWARM", "AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_SWARM_TASK"):
        monkeypatch.delenv(name, raising=False)
    with patch("hooks._redis.get_redis", return_value=None):
        yield


def _bash(command: str, cwd: str = "") -> dict[str, object]:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}


def _nested(command: str, depth: int) -> str:
    for _ in range(depth):
        command = f"bash -c {shlex.quote(command)}"
    return command


@pytest.mark.feature("agent-sessions-cannot-approve-pull-requests")
@pytest.mark.parametrize(
    "command",
    [
        "gh pr review 12 --approve",
        "gh pr review --approve 12 --body 'ship it'",
        "gh pr review -a",
        "gh pr review 12 -ab ok",
        "gh pr review 12 --approve=true",
        "gh pr -R owner/repo review 12 --approve",
        "gh -R owner/repo pr review 12 --approve",
        "gh pr --repo owner/repo review 12 --approve",
        "/usr/bin/gh pr review 12 --approve",
        "env GH_PROMPT_DISABLED=1 gh pr review 12 --approve",
        "bash -c 'gh pr review 12 --approve'",
        "cd /tmp && gh pr review 12 --approve",
        "echo 12 | xargs -n 1 gh pr review --approve",
        "find . -name x -exec gh pr review 12 --approve ';'",
        _nested("gh pr review 12 --approve", 10),
    ],
)
def test_gh_pr_review_with_approve_is_refused(command: str) -> None:
    with pytest.raises(BlockAction, match=REFUSED):
        hook_manager._check_shell_guards(_bash(command))


@pytest.mark.feature("agent-sessions-cannot-approve-pull-requests")
@pytest.mark.parametrize(
    "command",
    [
        "gh api repos/o/r/pulls/12/reviews -f event=APPROVE",
        "gh api -X POST 'repos/{owner}/{repo}/pulls/12/reviews' -F event=approve -f body=ok",
        "gh api repos/o/r/pulls/12/reviews/99/events -f event=APPROVE",
        "gh api --hostname github.com repos/o/r/pulls/12/reviews --field=event=APPROVE",
        'echo "{\\"event\\":\\"APPROVE\\"}" | gh api repos/o/r/pulls/12/reviews --input -',
        'echo \'{"event":"APPROVE"}\' | curl -X POST https://api.github.com/repos/o/r/pulls/12/reviews -d @-',
        'echo \'{"event":"APPROVE"}\' | gh api repos/o/r/pulls/12/reviews --input=-',
        'echo \'{"event":"APPROVE"}\' | curl https://api.github.com/repos/o/r/pulls/12/reviews --data-binary=@-',
        'gh api repos/o/r/pulls/12/reviews --input - <<\'EOF\'\n{"event": "APPROVE", "body": "ok"}\nEOF',
        'echo \'{"event":"APPROVE"}\' | gh api repos/o/r/pulls/12/reviews --input -',
        'curl -X POST https://api.github.com/repos/o/r/pulls/12/reviews -d \'{"event":"APPROVE"}\'',
    ],
)
def test_a_rest_review_with_an_approve_event_is_refused(command: str) -> None:
    with pytest.raises(BlockAction, match=REFUSED):
        hook_manager._check_shell_guards(_bash(command))


@pytest.mark.feature("agent-sessions-cannot-approve-pull-requests")
@pytest.mark.parametrize(
    "command",
    [
        "gh api repos/o/r/pulls/12/reviews --input review.json",
        "gh api repos/o/r/pulls/12/reviews --input=review.json",
        "curl -X POST https://api.github.com/repos/o/r/pulls/12/reviews -d @review.json",
        "gh api repos/o/r/pulls/12/reviews -F event=@event.txt",
        "curl -X POST https://api.github.com/repos/o/r/pulls/12/reviews -d@review.json",
    ],
)
def test_a_rest_review_whose_input_file_approves_is_refused(tmp_path: Path, command: str) -> None:
    (tmp_path / "review.json").write_text('{"event": "APPROVE", "body": "ok"}')
    (tmp_path / "event.txt").write_text("APPROVE\n")
    with pytest.raises(BlockAction, match=REFUSED):
        hook_manager._check_shell_guards(_bash(command, str(tmp_path)))


@pytest.mark.feature("agent-sessions-cannot-approve-pull-requests")
@pytest.mark.parametrize(
    "command",
    [
        'gh api graphql -f query=\'mutation { addPullRequestReview(input: {pullRequestId: "PR_1", event: APPROVE})'
        " { clientMutationId } }'",
        "gh api graphql -f query='mutation($id: ID!) { submitPullRequestReview(input: {pullRequestReviewId: $id,"
        " event: APPROVE}) { clientMutationId } }' -f id=PRR_1",
        "gh api graphql -f query='mutation($id: ID!, $e: PullRequestReviewEvent!) { addPullRequestReview(input:"
        " {pullRequestId: $id, event: $e}) { clientMutationId } }' -f id=PR_1 -f e=APPROVE",
        'curl https://api.github.com/graphql -d \'{"query": "mutation { submitPullRequestReview(input:'
        " {pullRequestReviewId: 1, event: APPROVE}) { clientMutationId } }\"}'",
        'curl https://api.github.com/graphql -d \'{"query": "mutation($e: PullRequestReviewEvent!) { addPullRequestReview('
        'input: {pullRequestId: \\"PR_1\\", event: $e}) { clientMutationId } }", "variables": {"e": "APPROVE"}}\'',
    ],
)
def test_a_graphql_review_with_an_approve_event_is_refused(command: str) -> None:
    with pytest.raises(BlockAction, match=REFUSED):
        hook_manager._check_shell_guards(_bash(command))


@pytest.mark.feature("agent-sessions-cannot-approve-pull-requests")
def test_a_graphql_review_whose_query_file_approves_is_refused(tmp_path: Path) -> None:
    (tmp_path / "review.graphql").write_text(
        'mutation { addPullRequestReview(input: {pullRequestId: "PR_1", event: APPROVE}) { clientMutationId } }'
    )
    with pytest.raises(BlockAction, match=REFUSED):
        hook_manager._check_shell_guards(_bash("gh api graphql -F query=@review.graphql", str(tmp_path)))


@pytest.mark.feature("agent-sessions-cannot-approve-pull-requests")
@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("mcp__github__create_pull_request_review", {"owner": "o", "repo": "r", "pullNumber": 12, "event": "APPROVE"}),
        ("mcp__github__submit_pending_pull_request_review", {"pullNumber": 12, "event": "approve"}),
        ("mcp__github__pull_request_review_write", {"method": "create", "pullNumber": 12, "event": "APPROVE"}),
        ("mcp__gateway-tools__github-create_pull_request_review", {"pullNumber": 12, "event": "Approve"}),
    ],
)
def test_a_github_mcp_review_with_an_approve_event_is_refused(tool_name: str, tool_input: dict[str, object]) -> None:
    with pytest.raises(BlockAction, match=REFUSED):
        hook_manager._check_shell_guards({"tool_name": tool_name, "tool_input": tool_input})


@pytest.mark.feature("agent-sessions-cannot-approve-pull-requests")
@pytest.mark.parametrize(
    "command",
    [
        "gh pr review 12 --comment --body 'needs a test'",
        "gh pr review 12 --request-changes -b 'fix the guard'",
        "gh pr review 12 -c -b approve",
        "gh pr review 12 -rb a",
        "gh pr view 12 --json reviews",
        "gh api repos/o/r/pulls/12/reviews -f event=COMMENT -f body='I approve of the approach'",
        "gh api repos/o/r/pulls/12/reviews -f event=REQUEST_CHANGES -f body='approve after the fix'",
        "gh api repos/o/r/pulls/12/comments -f body='event=APPROVE'",
        "gh pr review 12 --comment -b '-a fine'",
        "gh api repos/o/r/pulls/12/comments -f event=APPROVE -f body=ok",
        'gh api graphql -f query=\'mutation { addPullRequestReviewComment(input: {body: "event: APPROVE"})'
        " { clientMutationId } }'",
        "gh api repos/o/r/pulls/12/reviews -f event=COMMENT && echo 'event=APPROVE'",
        "gh api repos/o/r/pulls/12/reviews -f event=COMMENT -f body='event: approve'",
        "gh api repos/o/r/pulls/12/reviews -f body=APPROVE -f event=COMMENT",
        "gh api repos/o/r/pulls/12/reviews -f event=COMMENT -f body='event=APPROVE'",
        "echo 'event=APPROVE' && gh api repos/o/r/pulls/12/reviews -f event=COMMENT",
        "gh api repos/o/r/pulls/12/reviews/99/comments -f body='event=APPROVE'",
        "gh api repos/o/r/pulls/12/reviews -F event=@missing.txt",
        'gh api graphql -f query=\'mutation($b: String!) { addPullRequestReview(input: {pullRequestId: "PR_1",'
        " event: COMMENT, body: $b}) { clientMutationId } }' -f b=APPROVE",
        "gh api repos/o/r/pulls/12/reviews --input missing.json",
        'gh api graphql -f query=\'mutation { addPullRequestReview(input: {pullRequestId: "PR_1", event: COMMENT,'
        ' body: "approve?"}) { clientMutationId } }\'',
        'gh api graphql -f query=\'query { repository(owner: "o", name: "r") { pullRequest(number: 1)'
        " { reviews(states: APPROVED) { totalCount } } } }'",
        "gh api graphql -f query='mutation { addPullRequestReviewThread(input: {body: \"x\"}) { clientMutationId } }'"
        " -f event=APPROVE",
        "git commit -m 'Refuse gh pr review --approve and addPullRequestReview with event: APPROVE'",
        "gh pr create --base dev --title 'Refuse approvals' --body 'gh api repos/o/r/pulls/1/reviews -f event=APPROVE'",
    ],
)
def test_comments_change_requests_and_mentions_pass(command: str) -> None:
    hook_manager._check_shell_guards(_bash(command))


@pytest.mark.feature("agent-sessions-cannot-approve-pull-requests")
def test_an_input_file_resolves_against_the_working_directory_when_the_call_names_no_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "review.json").write_text('{"event": "APPROVE"}')
    monkeypatch.chdir(tmp_path)
    command = {"command": "gh api repos/o/r/pulls/12/reviews --input review.json"}
    with pytest.raises(BlockAction, match=REFUSED):
        hook_manager._check_shell_guards({"tool_name": "Bash", "tool_input": command})


@pytest.mark.feature("agent-sessions-cannot-approve-pull-requests")
@pytest.mark.parametrize(
    ("command", "refused"),
    [
        (_nested("gh pr review 12 --approve", 11), True),
        (_nested("gh api repos/o/r/pulls/12/reviews -f event=APPROVE", 11), True),
        (_nested("gh pr review 12 --comment -b ok", 11), False),
    ],
)
def test_a_command_too_deep_to_parse_is_refused_when_it_names_an_approval(command: str, refused: bool) -> None:
    assert (refusal(_bash(command)) is not None) is refused


@pytest.mark.feature("agent-sessions-cannot-approve-pull-requests")
@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("mcp__github__create_pull_request_review", {"pullNumber": 12, "event": "COMMENT", "body": "approve"}),
        ("mcp__github__create_pull_request_review", {"pullNumber": 12, "event": "REQUEST_CHANGES"}),
        ("mcp__github__create_pull_request_review", {"pullNumber": 12, "body": "APPROVE"}),
        ("mcp__jira__transition_issue", {"issue": "X-1", "event": "APPROVE"}),
    ],
)
def test_github_mcp_comments_and_other_tools_pass(tool_name: str, tool_input: dict[str, object]) -> None:
    hook_manager._check_shell_guards({"tool_name": tool_name, "tool_input": tool_input})
