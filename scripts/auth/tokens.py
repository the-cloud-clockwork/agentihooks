"""Short lived signed tokens: the auth service signs them and other services verify them locally with a shared key."""

import time
from collections.abc import Mapping

import jwt

KEY_ENV = "AGENTIHOOKS_AUTH_KEY"
LEGACY_ENV = "AGENTIHOOKS_AUTH_LEGACY"
TTL_S = 900
ALGORITHM = "HS256"


def key(environ: Mapping[str, str]) -> str:
    """The shared signing key, empty when the deployment has none."""
    return environ.get(KEY_ENV, "")


def legacy(environ: Mapping[str, str]) -> bool:
    """Whether services still accept today's credentials beside signed tokens."""
    return environ.get(LEGACY_ENV, "on") != "off"


def sign(secret: str, principal: str, slug: str, now: float | None = None) -> str:
    """A token naming the principal ('' for the operator) on one ledger, valid for TTL_S seconds."""
    issued = int(time.time() if now is None else now)
    claims = {"sub": principal, "slug": slug, "iat": issued, "exp": issued + TTL_S}
    return jwt.encode(claims, secret)


def verify(secret: str, token: str, slug: str) -> str | None:
    """The token's principal when it is signed with the key, unexpired and issued for this ledger, else None."""
    if not secret:
        return None
    try:
        claims = jwt.decode(token, secret, algorithms=[ALGORITHM], options={"require": ["sub", "slug", "exp"]})
    except jwt.PyJWTError:
        return None
    return claims["sub"] if claims["slug"] == slug else None
