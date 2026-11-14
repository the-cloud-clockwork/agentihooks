"""Remote branch heads the hub reads through the GitHub API, since it holds no checkout of the swarm repository."""

import subprocess
from collections.abc import Callable, Collection
from functools import partial
from typing import Any

from scripts.handoff import envelope
from scripts.swarm import merge_queue, stack
from scripts.swarm.naming import plain
from scripts.swarm.store import SwarmConfig, SwarmError


def heads(origin: str, branches: Collection[str], run: Callable[..., Any] = subprocess.run) -> dict[str, str]:
    """Each branch's head on the origin repository in one query: none when absent, unknown when GitHub was not read."""
    names = list(dict.fromkeys(branches))
    if not names:
        return {}
    owner, _, repo = origin.partition("/")[2].partition("/")
    declared = "".join(f", $b{index}: String!" for index in range(len(names)))
    refs = " ".join(f"b{index}: ref(qualifiedName: $b{index}) {{ target {{ oid }} }}" for index in range(len(names)))
    query = (
        f"query($owner: String!, $name: String!{declared}) {{ repository(owner: $owner, name: $name) {{ {refs} }} }}"
    )
    variables = {
        "owner": owner,
        "name": repo,
        **{f"b{index}": f"refs/heads/{name}" for index, name in enumerate(names)},
    }
    try:
        found = merge_queue.graphql(query, variables, run)["repository"]
    except SwarmError:
        found = None
    if not isinstance(found, dict):
        return dict.fromkeys(names, envelope.UNKNOWN)
    return {name: _oid(found.get(f"b{index}")) for index, name in enumerate(names)}


def _oid(ref: Any) -> str:
    return ref["target"]["oid"] if ref else envelope.NONE


def reclaimer(config: SwarmConfig) -> tuple[Callable[..., dict], str]:
    """The reclaim a hub continuation resolves with and the repository key it compares task branches against."""
    if not config.repo_origin:
        return partial(envelope.reclaim, heads=envelope.remote_heads(config.repo)), stack.origin_key(config.repo)

    def reclaim(repo: str, lives: list[str], recorded: str, forbidden: Collection[str] = ()) -> dict:
        checked = [recorded] if recorded else [plain(name) for name in lives]
        found = heads(config.repo_origin, checked)
        return envelope.reclaim(repo, lives, recorded, forbidden=forbidden, heads=found)

    return reclaim, stack.repo_key(config.repo_origin)
