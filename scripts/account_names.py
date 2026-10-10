import re

TOKEN_PREFIX = "AH_CC_TOKEN_"
SYMBOL = re.compile(r"[^a-z0-9]")


def secret_key(email: str) -> str:
    return SYMBOL.sub("-", email.lower().replace("@", "at"))


def token_env(email: str) -> str:
    return TOKEN_PREFIX + secret_key(email).replace("-", "_")
