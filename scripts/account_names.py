"""One account email names its Kubernetes secret key and its AH_CC_TOKEN_ environment variable."""

import re

TOKEN_PREFIX = "AH_CC_TOKEN_"
SYMBOL = re.compile(r"[^a-z0-9]")


def secret_key(email: str) -> str:
    return SYMBOL.sub("-", email.lower().replace("@", "at"))


def env_slug(email: str) -> str:
    return secret_key(email).replace("-", "_")


def token_env(email: str) -> str:
    return TOKEN_PREFIX + env_slug(email)
