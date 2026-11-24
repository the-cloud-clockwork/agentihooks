import base64
import json
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from scripts.ci_lab.plant_proofs import IDENTITY, Pair
from scripts.ci_lab.remote.checkout import at
from scripts.ci_lab.webhooks import PLANT_PREFIX

Request = Callable[[str, str, Mapping[str, Any] | None], Any]


def _api(method: str, path: str, body: Mapping[str, Any] | None) -> Any:
    arguments = ["gh", "api", path, "--method", method]
    if body is not None:
        arguments.extend(["--input", "-"])
    result = subprocess.run(
        arguments,
        input=json.dumps(body) if body is not None else None,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def _git(root: Path, *arguments: str) -> str:
    result = at(root)(*arguments)
    result.check_returncode()
    return result.stdout


def _put(request: Request, path: str, body: Mapping[str, Any], expected: str) -> None:
    if request("POST", path, body).get("sha") != expected:
        raise ValueError(f"unexpected Git object identity for {path}")


def _tree(root: Path, pair: Pair, head: str, endpoint: str, request: Request) -> str:
    tree = _git(root, "rev-parse", f"{head}^{{tree}}").strip()
    paths = _git(root, "diff", "--name-only", "-z", pair.base, head).split("\0")
    entries: list[dict[str, str]] = []
    for path in filter(None, paths):
        mode, kind, sha = _git(root, "ls-tree", "-z", head, "--", path).split("\t", 1)[0].split()
        content = subprocess.run(
            ["git", "-C", str(root), "cat-file", "blob", sha], capture_output=True, check=True, timeout=30
        ).stdout
        _put(request, f"{endpoint}/blobs", {"content": base64.b64encode(content).decode(), "encoding": "base64"}, sha)
        entries.append({"path": path, "mode": mode, "type": kind, "sha": sha})
    if entries:
        base = _git(root, "rev-parse", f"{pair.base}^{{tree}}").strip()
        _put(request, f"{endpoint}/trees", {"base_tree": base, "tree": entries}, tree)
    return tree


def _commit(root: Path, head: str, tree: str, endpoint: str, request: Request) -> None:
    message = _git(root, "cat-file", "commit", head).split("\n\n", 1)[1]
    parent = _git(root, "rev-parse", f"{head}^").strip()
    body: dict[str, Any] = {"message": message, "tree": tree, "parents": [parent]}
    for role in ("author", "committer"):
        body[role] = {field: IDENTITY[f"GIT_{role.upper()}_{field.upper()}"] for field in ("name", "email", "date")}
    _put(request, f"{endpoint}/commits", body, head)


def publish(root: Path, pair: Pair, head: str, repository: str, request: Request | None = None) -> None:
    request = request or _api
    endpoint = f"repos/{repository.removeprefix('https://github.com/')}/git"
    branch = f"{PLANT_PREFIX}{pair.name}-{pair.base}"
    rows = request("GET", f"{endpoint}/matching-refs/{branch.removeprefix('refs/')}", None)
    matches = [row for row in rows if row["ref"] == branch]
    if matches:
        tip = matches[0]["object"]["sha"]
        if tip in {head, pair.red}:
            return
        if tip != pair.green or head != pair.red:
            raise ValueError(f"divergent plant ref {branch}")
    tree = _tree(root, pair, head, endpoint, request)
    _commit(root, head, tree, endpoint, request)
    if matches:
        result = request("PATCH", f"{endpoint}/{branch}", {"sha": head, "force": False})
    else:
        result = request("POST", f"{endpoint}/refs", {"ref": branch, "sha": head})
    if result["object"]["sha"] != head:
        raise ValueError(f"unexpected ref identity for {branch}")
