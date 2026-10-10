"""The Swarm panel's controls on the command line: the operator, or the live master of that same swarm, and nobody else."""

import os

from scripts.swarm.store import DISPATCH, FULL, MASTER, SwarmError

OPERATOR = "operator"
COMMANDS = frozenset({"start", "pause", "stop", "close", "reopen", "set", "lift"})


def holder(store, slug, who, environ=None):
    env = os.environ if environ is None else environ
    name = store.names.resolve(who.name)
    if _seated(store, slug, who, MASTER):
        return name
    agent = any(a.name == name for other in store.slugs() for a in store.agents(other))
    if not (who.swarm or agent) and (env.get("AGENTIHOOKS_AGENT_NAME") or OPERATOR) == OPERATOR:
        return OPERATOR
    raise SwarmError(f"only the operator or the master of swarm {slug} uses its swarm controls, and {name} is neither")


def _seated(store, slug, who, lane):
    name = store.names.resolve(who.name)
    here = not who.swarm or store.names.swarm_slug(who.swarm) == slug
    return here and any(a.lane == lane and a.state != "finished" and a.name == name for a in store.agents(slug))


def freezer(store, slug, who):
    if not _seated(store, slug, who, DISPATCH):
        return holder(store, slug, who)
    autonomy = store.config(slug).autonomy
    if autonomy != FULL:
        raise SwarmError(
            f"the dispatcher of swarm {slug} writes freezes only at full autonomy, and its autonomy is {autonomy}"
        )
    return store.names.resolve(who.name)


def record(ledger, slug, name, before, after):
    changes = ", ".join(f"{control} from {before[control]} to {after[control]}" for control in before)
    text = f"{name} changed {changes}."
    ledger.notify(slug, text.replace("-", " ").replace("@", " "))
