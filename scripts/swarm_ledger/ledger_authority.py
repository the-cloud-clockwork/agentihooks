"""Who a ledger request acts for: the operator's page credential, or one agent's credential bound to ledger and name.

A pinned swarm session sends the agent credential, so the server derives its author and role from the binding. The
binding governs the transport a session selects; any process of the operator's user can read the page credential.
"""

import hashlib
import hmac
import time
from functools import lru_cache
from os import environ

import jwt

from scripts.swarm_ledger import ports
from scripts.swarm_ledger.ledger_link import remote


def resolve_name(name):
    return ports.swarm().naming.resolve_name(name)


def connect(environ=None):
    return ports.swarm().store.connect(environ)


def agent_token(admin, slug, name):
    return hmac.new(admin.encode(), f"{slug}\n{name}".encode(), hashlib.sha256).hexdigest()


def _same(given, expected):
    return hmac.compare_digest(given.encode(), expected.encode())


TICK_TTL = 900


def tick_token(secret: str, slug: str, expires: int) -> str:
    message = f"ledger-tick:{slug}:{expires}".encode()
    return f"{expires}.{hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()}"


def tick_principal(token: str, slug: str) -> str | None:
    secret = environ.get("SWARM_SECRET")
    if not secret:
        return None
    try:
        expires = int(token.split(".", 1)[0])
    except ValueError:
        return None
    if not 0 < expires - int(time.time()) <= TICK_TTL:
        return None
    return "" if _same(token, tick_token(secret, slug, expires)) else None


def principal(admin: str, slug: str, token: str, agent: str) -> str | None:
    """'' for the operator, the agent's name for a bound agent, None for a refused credential."""
    if not admin or not token:
        return None
    if _same(token, admin):
        return ""
    if token.startswith("hive."):
        return hive_principal(slug, token, agent)
    if not remote() and agent and _same(token, agent_token(admin, slug, agent)):
        return agent
    return None


def hive_principal(slug: str, token: str, agent: str) -> str | None:
    member = ports.hive().auth.agent_member(ports.swarm().store.redis_client(), token, slug, agent)
    return agent if member and hive_agent(member, slug, agent) else None


def caller(headers, admin: str, slug: str) -> str | None:
    """principal for this request under SWARM_AUTH_MODE: local reads the page credential, edge never does."""
    token, agent = headers.get("X-Ledger-Token"), headers.get("X-Ledger-Agent")
    if environ.get("SWARM_AUTH_MODE") != "edge":
        return principal(admin, slug, token, agent)
    tick = headers.get("X-Ledger-Tick")
    if tick is not None:
        return tick_principal(tick, slug)
    if access_operator(headers.get("Cf-Access-Jwt-Assertion")):
        return ""
    if not token or not agent:
        return None
    if token.startswith("hive."):
        return hive_principal(slug, token, agent)
    secret = environ.get("SWARM_SECRET")
    if not secret:
        return None
    return agent if _same(token, agent_token(secret, slug, agent)) else None


def access_operator(assertion) -> bool:
    team = environ.get("SWARM_ACCESS_TEAM", "").removeprefix("https://")
    audience = environ.get("SWARM_ACCESS_AUD")
    if not assertion or not team or not audience:
        return False
    issuer = f"https://{team if '.' in team else team + '.cloudflareaccess.com'}"
    try:
        key = access_keys(f"{issuer}/cdn-cgi/access/certs").get_signing_key_from_jwt(assertion)
        jwt.decode(
            assertion,
            key.key,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "iat"]},
        )
    except (jwt.PyJWTError, OSError, ValueError):
        return False
    return True


@lru_cache(maxsize=4)
def access_keys(url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(url, cache_keys=True, timeout=10)


def hive_member(credential):
    return ports.hive().auth.ledger_member(ports.swarm().store.redis_client(), credential) if credential else None


def hive_agent(member: str, slug: str, name: str) -> bool:
    from scripts.doctor.priming import doctor_slug

    store = ports.swarm().store.connect()
    return any(
        agent.name == name and agent.hive == member
        for ledger in (slug, doctor_slug(slug))
        for agent in store.agents(ledger)
    )


def hive_agent_token(member: str, slug: str, name: str) -> str:
    return ports.hive().auth.issue_agent(ports.swarm().store.redis_client(), member, slug, name)


def controller(credential):
    return bool(credential) and ports.hive().auth.controller(ports.swarm().store.redis_client(), credential)


def refusal(name, op):
    if not name:
        return ""
    by = op.get("by")
    if by is None:
        return "only the operator writes without an author"
    if by != name and resolve_name(by) != resolve_name(name):
        return f"{name} cannot write as {by}"
    if (
        op["op"] == "join"
        and op.get("role", "member") != "member"
        and ports.swarm().naming.lane_of(resolve_name(name)) != "master"
    ):
        return f"{name} cannot join as {op['role']}"
    if op["op"] == "claim":
        return claim_refusal(resolve_name(name))
    if "to" in op and str(op.get("thread")).endswith("/comments"):
        return f"{name} cannot answer the operator on an entry; the swarm relay posts that answer"
    return ""


def claim_refusal(by):
    from redis import RedisError

    NameRegistry = ports.swarm().naming.NameRegistry

    try:
        launched = NameRegistry(ports.swarm().store.redis_client()).entry(by).get("operator")
    except RedisError:
        return ""
    return f"{by} was launched with swarm profile up and claims no item" if launched else ""


def fence(slug: str, epoch: int) -> None:
    lease = ports.swarm().lease

    lease.require_epoch(connect(), slug, epoch)
