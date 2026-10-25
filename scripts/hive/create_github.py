"""agentihooks hive create: read antoncore files and land the rendered hive by one pull request, through gh."""

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence

from scripts.hive import auth

Run = Callable[..., subprocess.CompletedProcess]
GH_TIMEOUT_S = 120
RAW = "Accept: application/vnd.github.raw"
AUTO_REFUSED = "auto merge is not allowed"
PASSED = ("SUCCESS", "NEUTRAL", "SKIPPED")
WAITING = (None, "PENDING", "EXPECTED")
REQUIRED = '[.[] | select(.type == "required_status_checks") | .parameters.required_status_checks[].context]'


def gh(run: Run, argv: list[str], body: dict | None = None) -> str:
    stdin = None if body is None else json.dumps(body)
    try:
        done = run(["gh", *argv], input=stdin, capture_output=True, text=True, timeout=GH_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise auth.HiveError(f"gh {argv[0]} failed ({exc})") from exc
    if done.returncode != 0:
        raise auth.HiveError(f"gh {' '.join(argv[:2])} exited {done.returncode}: {done.stderr.strip()}")
    return done.stdout.rstrip("\n")


def read(run: Run, repo: str, path: str, ref: str) -> str:
    return gh(run, ["api", "-H", RAW, f"repos/{repo}/contents/{path}?ref={ref}"]) + "\n"


def open_pr(run: Run, repo: str, base: str, branch: str, files: Mapping[str, str], message: str) -> str:
    pending = '[.[] | select(.state != "CLOSED")][0].url // ""'
    found = gh(
        run, ["pr", "list", "-R", repo, "--head", branch, "--state", "all", "--json", "url,state", "--jq", pending]
    )
    if found:
        return found
    head = gh(run, ["api", f"repos/{repo}/git/ref/heads/{base}", "--jq", ".object.sha"])
    tree = gh(run, ["api", f"repos/{repo}/git/commits/{head}", "--jq", ".tree.sha"])
    entries = [{"path": path, "mode": "100644", "type": "blob", "content": text} for path, text in files.items()]
    tree = _post(run, f"repos/{repo}/git/trees", {"base_tree": tree, "tree": entries})
    commit = _post(run, f"repos/{repo}/git/commits", {"message": message, "tree": tree, "parents": [head]})
    _branch(run, repo, branch, commit)
    title = message.splitlines()[0]
    return gh(run, ["pr", "create", "-R", repo, "--base", base, "--head", branch, "--title", title, "--body", message])


def _branch(run: Run, repo: str, branch: str, commit: str) -> None:
    try:
        _post(run, f"repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit})
    except auth.HiveError as exc:
        if "Reference already exists" not in str(exc):
            raise
        body = {"sha": commit, "force": True}
        gh(
            run,
            ["api", "-X", "PATCH", f"repos/{repo}/git/refs/heads/{branch}", "--input", "-", "--jq", ".object.sha"],
            body,
        )


def _post(run: Run, path: str, body: dict) -> str:
    return gh(run, ["api", "-X", "POST", path, "--input", "-", "--jq", ".sha"], body)


def pr_state(run: Run, url: str) -> str:
    return gh(run, ["pr", "view", url, "--json", "state", "--jq", ".state"])


def merge(run: Run, url: str) -> bool:
    try:
        gh(run, ["pr", "merge", url, "--squash", "--auto"])
    except auth.HiveError as exc:
        if AUTO_REFUSED not in str(exc).lower():
            raise
        return False
    return True


def required_checks(run: Run, repo: str, base: str) -> list[str]:
    return json.loads(gh(run, ["api", f"repos/{repo}/rules/branches/{base}", "--jq", REQUIRED]))


def checks_passed(run: Run, url: str, required: Sequence[str] = ()) -> bool:
    rollup = json.loads(gh(run, ["pr", "view", url, "--json", "statusCheckRollup"]))["statusCheckRollup"] or []
    results = [
        (check.get("name") or check.get("context"), check.get("conclusion") or check.get("state")) for check in rollup
    ]
    if required:
        latest = dict(results)
        results = [(name, latest.get(name)) for name in required]
    failed = [name for name, result in results if result not in (*PASSED, *WAITING)]
    if failed:
        raise auth.HiveError(
            f"pull request {url} has failed checks: {', '.join(failed)}; fix them and rerun hive create"
        )
    return bool(results) and all(result in PASSED for _, result in results)


def squash(run: Run, url: str) -> None:
    gh(run, ["pr", "merge", url, "--squash"])
