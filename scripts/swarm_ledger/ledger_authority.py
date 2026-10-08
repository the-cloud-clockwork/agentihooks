"""Who a ledger request acts for: the operator's page credential, or one agent's credential bound to ledger and name.

A pinned swarm session sends the agent credential, so the server derives its author and role from the binding. The
binding governs the transport a session selects; any process of the operator's user can read the page credential.
"""

import hashlib
import hmac

from scripts.hive import auth
from scripts.swarm import store
from scripts.swarm.naming import lane_of, resolve_name
from scripts.swarm.store import connect


def agent_token(admin, slug, name):
    return hmac.new(admin.encode(), f"{slug}\n{name}".encode(), hashlib.sha256).hexdigest()


def _same(given, expected):
    return hmac.compare_digest(given.encode(), expected.encode())


def principal(admin, slug, token, agent):
    """'' for the operator, the agent's name for a bound agent, None for a refused credential."""
    if not admin or not token:
        return None
    if _same(token, admin):
        return ""
    if agent and _same(token, agent_token(admin, slug, agent)):
        return agent
    return None


def hive_member(credential):
    return auth.ledger_member(store.redis_client(), credential) if credential else None


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


def fence(slug: str, epoch: int) -> None:
    from scripts.swarm import lease

    lease.require_epoch(connect(), slug, epoch)
