"""An accepted plan published where engineers pick it up: a GitHub issue where the repo has issues, else a ledger artifact."""

import json
import re
import subprocess

HEADING_RE = re.compile(r"^#\s+(.+)$", re.M)
NO_REPO = ("not a git repository", "point to a known GitHub host")
REFUSED_NOTE = "The ledger refused this plan publish, so its issue is closed until the same plan is published again."


class PublishError(RuntimeError):
    pass


def title_of(text: str, phases: list[str]) -> str:
    match = HEADING_RE.search(text)
    return match.group(1).strip() if match else f"Plan for phases {', '.join(phases)}"


def has_issues(repo: str, run=subprocess.run) -> bool:
    done = run(
        ["gh", "repo", "view", *([repo] if repo else []), "--json", "hasIssuesEnabled"], capture_output=True, text=True
    )
    if done.returncode and any(marker in done.stderr for marker in NO_REPO):
        return False
    if done.returncode:
        raise PublishError(f"gh repo view failed: {(done.stderr or done.stdout).strip()}")
    return json.loads(done.stdout).get("hasIssuesEnabled") is True


def open_issue(path: str, title: str, repo: str, run=subprocess.run) -> str:
    argv = [
        "gh",
        "issue",
        "create",
        "--title",
        title,
        "--body",
        f"{title}\n\n{path}",
        *(["--repo", repo] if repo else []),
    ]
    return gh_issue(argv, run).strip().splitlines()[-1]


def reused_issue(path: str, repo: str, run=subprocess.run) -> str:
    argv = [
        "gh",
        "issue",
        "list",
        "--state",
        "all",
        "--search",
        f'"{path}" in:body',
        "--json",
        "url,state,body,comments",
        *(["--repo", repo] if repo else []),
    ]
    found = [issue for issue in json.loads(gh_issue(argv, run)) if path in issue["body"]]
    if issue := next((issue for issue in found if issue["state"] == "OPEN"), None):
        return issue["url"]
    issue = next(
        (issue for issue in found if issue["comments"][-1:] and issue["comments"][-1]["body"] == REFUSED_NOTE), None
    )
    if issue is None:
        return ""
    gh_issue(["gh", "issue", "reopen", issue["url"]], run)
    return issue["url"]


def close_issue(url: str, run=subprocess.run) -> None:
    gh_issue(["gh", "issue", "comment", url, "--body", REFUSED_NOTE], run)
    gh_issue(["gh", "issue", "close", url], run)


def gh_issue(argv: list[str], run) -> str:
    done = run(argv, capture_output=True, text=True)
    if done.returncode:
        raise PublishError(f"gh issue {argv[2]} failed: {(done.stderr or done.stdout).strip()}")
    return done.stdout


def publish(path: str, title: str, repo: str, artifact, run=subprocess.run, *, issue_title: str) -> tuple[str, str]:
    issues = has_issues(repo, run)
    url = artifact(path, title)
    if issues:
        return reused_issue(url, repo, run) or open_issue(url, issue_title, repo, run), "issue"
    return url, "artifact"
