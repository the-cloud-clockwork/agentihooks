"""Remote branch heads the hub reads through the GitHub API, since it holds no checkout of the swarm repository."""

import subprocess
from collections.abc import Callable, Collection
from functools import partial
from typing import Any, cast

from scripts.handoff import envelope as handoff_envelope
from scripts.swarm import merge_queue, naming, stack
from scripts.swarm.store import SwarmConfig, SwarmError

envelope: Any = handoff_envelope
plain: Callable[[str], str] = cast(Any, naming).plain


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
        return {name: str(envelope.UNKNOWN) for name in names}
    answered: dict[str, Any] = cast(dict[str, Any], found)
    return {name: _oid(answered.get(f"b{index}")) for index, name in enumerate(names)}


def _oid(ref: Any) -> Any:
    return ref["target"]["oid"] if ref else envelope.NONE


def reclaimer(config: SwarmConfig) -> tuple[Callable[..., dict[str, Any]], str]:
    """The reclaim a hub continuation resolves with and the repository key it compares task branches against."""
    if not config.repo_origin:
        return partial(envelope.reclaim, heads=envelope.remote_heads(config.repo)), stack.origin_key(config.repo)

    def reclaim(repo: str, lives: list[str], recorded: str, forbidden: Collection[str] = ()) -> dict[str, Any]:
        checked = [recorded] if recorded else [plain(name) for name in lives]
        found = heads(config.repo_origin, checked)
        verdict: dict[str, Any] = envelope.reclaim(repo, lives, recorded, forbidden=forbidden, heads=found)
        return verdict

    return reclaim, stack.repo_key(config.repo_origin)
