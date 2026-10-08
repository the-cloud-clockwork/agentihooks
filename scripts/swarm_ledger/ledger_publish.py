"""An accepted plan published where engineers pick it up: a GitHub issue where the repo has issues, else a ledger artifact."""

import json
import re
import subprocess

HEADING_RE = re.compile(r"^#\s+(.+)$", re.M)
NO_REPO = ("not a git repository", "point to a known GitHub host")


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
    argv = ["gh", "issue", "create", "--title", title, "--body", f"{title}\n\n{path}", *(["--repo", repo] if repo else [])]
    done = run(argv, capture_output=True, text=True)
    if done.returncode:
        raise PublishError(f"gh issue create failed: {(done.stderr or done.stdout).strip()}")
    return done.stdout.strip().splitlines()[-1]


def publish(path: str, title: str, repo: str, artifact, run=subprocess.run) -> tuple[str, str]:
    url = artifact(path, title)
    if has_issues(repo, run):
        return open_issue(url, title, repo, run), "issue"
    return url, "artifact"
