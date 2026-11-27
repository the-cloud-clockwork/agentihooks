"""Close the issues a merged ready pull request settles: GitHub closes them only on a merge into the default branch."""

import re
from collections.abc import Callable
from typing import Any

from scripts.swarm import stack
from scripts.swarm.store import SwarmError
from scripts.tick.deadline import Deadline

KEYWORD = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+#(\d+)\b", re.IGNORECASE)
COMMENT = "Closed by the swarm: pull request {url} merged into dev."


def named(url: str) -> tuple[list[str], list[str]]:
    """Return the issue urls a pull request body closes, or the action naming why the body could not be read."""
    try:
        body = stack.change(url, f"could not read the issues named by {url}").body
    except SwarmError as exc:
        return [], [str(exc)]
    repo = url.split("/pull/")[0]
    return [f"{repo}/issues/{number}" for number in KEYWORD.findall(body)], []


def unnamed(url: str, body: str, tasks: list[dict[str, Any]]) -> list[str]:
    """Return the issue urls ``body`` closes that no task in ``tasks`` records as its issue or names in its
    description, as ``#n`` or as the issue url."""
    repo = url.split("/pull/")[0]
    mention = re.compile(rf"(?:#|{re.escape(repo)}/issues/)(\d+)")
    named = {n for task in tasks for key in ("issue_url", "description") for n in mention.findall(str(task.get(key)))}
    return [f"{repo}/issues/{number}" for number in dict.fromkeys(KEYWORD.findall(body)) if number not in named]


def refuse_unnamed(url: str, body: str, row: dict[str, Any], task: Callable[[str], dict[str, Any] | None]) -> None:
    """Refuse recording ``url`` when its body closes an issue that the task ``row`` and its group members, read
    through ``task``, never name, since settle closes it on merge."""
    members: list[str] = row.get("group_members") or []
    tasks = [row, *(task(member) or {} for member in members)]
    if issues := unnamed(url, body, tasks):
        raise SwarmError(
            f"pull request {url} closes {', '.join(issues)}, which this task neither records nor names in its "
            "description: drop the closing keyword from the body, or record the task's own issue with swarm issue"
        )


def settle(url, finished, rows):
    recorded = [rows[task]["issue_url"] for task in finished if rows[task].get("issue_url")]
    held = {row.get("issue_url") for task, row in rows.items() if task not in finished and row.get("state") != "done"}
    issues, actions = named(url)
    for issue in dict.fromkeys([*recorded, *issues]):
        if issue not in held:
            actions += _close(issue, url)
    return actions


def _close(issue: str, url: str) -> list[str]:
    """Close an open issue through the source control port and say so on it, naming any step that failed."""
    try:
        provider, reference = stack.source_control(issue)
        if provider.issue_state(reference, deadline=Deadline.after(stack.PORT_SECONDS)) != "open":
            return []
        provider.close_issue(reference, deadline=Deadline.after(stack.PORT_SECONDS))
    except stack.PORT_ERRORS as exc:
        return [f"could not close issue {issue}: {stack.refusal(exc)}"]
    try:
        provider.comment(reference, COMMENT.format(url=url), deadline=Deadline.after(stack.PORT_SECONDS))
    except stack.PORT_ERRORS as exc:
        return [f"closed issue {issue}; could not comment on it: {stack.refusal(exc)}"]
    return [f"closed issue {issue}"]
