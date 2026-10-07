"""Who a ledger request acts for: the operator's page credential, or one agent's credential bound to ledger and name.

A pinned swarm session sends the agent credential, so the server derives its author and role from the binding.
"""

import hashlib
import hmac

from scripts.swarm.naming import lane_of, resolve_name


def agent_token(admin, slug, name):
    return hmac.new(admin.encode(), f"{slug}\n{name}".encode(), hashlib.sha256).hexdigest()


def principal(admin, slug, token, agent):
    """'' for the operator, the agent's name for a bound agent, None for a refused credential."""
    if not admin or not token:
        return None
    if token == admin:
        return ""
    if agent and token == agent_token(admin, slug, agent):
        return agent
    return None


def refusal(name, op):
    if not name:
        return ""
    by = op.get("by")
    if by is None:
        return "only the operator writes without an author"
    if by != name and resolve_name(by) != resolve_name(name):
        return f"{name} cannot write as {by}"
    if op["op"] == "join" and op.get("role", "member") != "member" and lane_of(resolve_name(name)) != "master":
        return f"{name} cannot join as {op['role']}"
    return ""
