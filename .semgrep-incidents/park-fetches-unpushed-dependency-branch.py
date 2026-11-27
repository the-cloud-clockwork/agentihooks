from typing import Any

from scripts.swarm.stack import _abandoned, _dev_base, _fetch_origin, _merged_into_dev, _out


def base_without_the_probe(dep: dict[str, Any], foreign: list[dict[str, Any]]) -> str:
    branch = str(dep["branch"])
    if _merged_into_dev(dep) or dep in foreign:
        return _dev_base()
    # ruleid: park-fetches-unpushed-dependency-branch
    _fetch_origin(["git"], branch, f"cannot fetch {branch}")
    return _out(["git", "merge-base", "HEAD", f"origin/{branch}"], f"{branch} shares no history")


def base_probing_another_branch(dep: dict[str, Any], other: str) -> str:
    branch = str(dep["branch"])
    if _abandoned(dep, other):
        return _dev_base()
    # ruleid: park-fetches-unpushed-dependency-branch
    _fetch_origin(["git"], branch, f"cannot fetch {branch}")
    return _out(["git", "merge-base", "HEAD", f"origin/{branch}"], f"{branch} shares no history")


def base_after_the_probe(dep: dict[str, Any], foreign: list[dict[str, Any]]) -> str:
    branch = str(dep["branch"])
    if _merged_into_dev(dep) or dep in foreign or _abandoned(dep, branch):
        return _dev_base()
    # ok: park-fetches-unpushed-dependency-branch
    _fetch_origin(["git"], branch, f"cannot fetch {branch}")
    return _out(["git", "merge-base", "HEAD", f"origin/{branch}"], f"{branch} shares no history")


def dev_base() -> str:
    # ok: park-fetches-unpushed-dependency-branch
    _fetch_origin(["git"], "dev", "cannot fetch dev")
    return _out(["git", "merge-base", "HEAD", "origin/dev"], "the task shares no history with dev")
