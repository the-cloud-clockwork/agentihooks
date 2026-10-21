"""agentihooks hive create: read antoncore files and land the rendered hive by one pull request, through gh."""

import json
import subprocess
from collections.abc import Callable, Mapping

from scripts.hive import auth

Run = Callable[..., subprocess.CompletedProcess]
GH_TIMEOUT_S = 120
RAW = "Accept: application/vnd.github.raw"


def gh(run: Run, argv: list[str], body: dict | None = None) -> str:
    stdin = None if body is None else json.dumps(body)
    try:
        done = run(["gh", *argv], input=stdin, capture_output=True, text=True, timeout=GH_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise auth.HiveError(f"gh {argv[0]} failed ({exc})") from exc
    if done.returncode != 0:
        raise auth.HiveError(f"gh {' '.join(argv[:2])} exited {done.returncode}: {done.stderr.strip()}")
    return done.stdout.strip()


def read(run: Run, repo: str, path: str, ref: str) -> str:
    return gh(run, ["api", "-H", RAW, f"repos/{repo}/contents/{path}?ref={ref}"]) + "\n"


def open_pr(run: Run, repo: str, base: str, branch: str, files: Mapping[str, str], message: str) -> str:
    found = gh(run, ["pr", "list", "-R", repo, "--head", branch, "--state", "all", "--json", "url", "--jq", ".[0].url"])
    if found:
        return found
    head = gh(run, ["api", f"repos/{repo}/git/ref/heads/{base}", "--jq", ".object.sha"])
    tree = gh(run, ["api", f"repos/{repo}/git/commits/{head}", "--jq", ".tree.sha"])
    entries = [{"path": path, "mode": "100644", "type": "blob", "content": text} for path, text in files.items()]
    tree = _post(run, f"repos/{repo}/git/trees", {"base_tree": tree, "tree": entries})
    commit = _post(run, f"repos/{repo}/git/commits", {"message": message, "tree": tree, "parents": [head]})
    _post(run, f"repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit})
    title = message.splitlines()[0]
    return gh(run, ["pr", "create", "-R", repo, "--base", base, "--head", branch, "--title", title, "--body", message])


def _post(run: Run, path: str, body: dict) -> str:
    return gh(run, ["api", "-X", "POST", path, "--input", "-", "--jq", ".sha"], body)


def merged(run: Run, url: str) -> bool:
    return gh(run, ["pr", "view", url, "--json", "state", "--jq", ".state"]) == "MERGED"


def merge(run: Run, url: str) -> None:
    gh(run, ["pr", "merge", url, "--squash", "--auto"])
