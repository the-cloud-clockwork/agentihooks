"""The Swarm panel's controls on the command line: the operator, or the live master of that same swarm, and nobody else."""

import os

from scripts.swarm.store import MASTER, SwarmError

OPERATOR = "operator"
COMMANDS = frozenset({"start", "pause", "stop", "close", "reopen", "set", "lift"})


def holder(store, slug, who, named="", environ=None):
    env = os.environ if environ is None else environ
    name = store.names.resolve(named or who.name)
    here = not who.swarm or store.names.swarm_slug(who.swarm) == slug
    if here and any(a.lane == MASTER and a.state != "finished" and a.name == name for a in store.agents(slug)):
        return name
    if not who.swarm and (named or env.get("AGENTIHOOKS_AGENT_NAME") or OPERATOR) == OPERATOR:
        return OPERATOR
    raise SwarmError(f"only the operator or the master of swarm {slug} uses its swarm controls, and {name} is neither")


def record(ledger, slug, name, before, after):
    changes = ", ".join(f"{control} from {before[control]} to {after[control]}" for control in before)
    text = f"{name} changed {changes}."
    ledger.say(slug, text.replace("-", " ").replace("@", " "), by="swarm")
