import subprocess
from typing import Any


def approve(number: str) -> None:
    # ruleid: agent-approves-pull-request
    subprocess.run(["gh", "pr", "review", number, "--approve"], check=True)


def comment(number: str) -> None:
    # ok: agent-approves-pull-request
    subprocess.run(["gh", "pr", "review", number, "--comment", "--body", "approve once the guard lands"], check=True)


def approve_payload() -> dict[str, str]:
    # ruleid: agent-approves-pull-request
    return {"event": "APPROVE", "body": "ok"}


def comment_payload() -> dict[str, str]:
    # ok: agent-approves-pull-request
    return {"event": "COMMENT", "body": "approve once the guard lands"}


def approve_field(number: str) -> list[str]:
    # ruleid: agent-approves-pull-request
    return ["gh", "api", f"repos/o/r/pulls/{number}/reviews", "-f", "event=APPROVE"]


def change_request_field(number: str) -> list[str]:
    # ok: agent-approves-pull-request
    return ["gh", "api", f"repos/o/r/pulls/{number}/reviews", "-f", "event=REQUEST_CHANGES"]


def approve_mutation() -> str:
    # ruleid: agent-approves-pull-request
    return "mutation($id: ID!) { submitPullRequestReview(input: {pullRequestReviewId: $id, event: APPROVE}) { clientMutationId } }"


def approve_call(pull: Any) -> None:
    # ruleid: agent-approves-pull-request
    pull.create_review(event="approve")


def comment_call(pull: Any) -> None:
    # ok: agent-approves-pull-request
    pull.create_review(event="COMMENT", body="approve once the guard lands")
