import re
from collections.abc import Mapping

TOKEN_PREFIX = "AH_CC_TOKEN_"
OAUTH_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
SYMBOL = re.compile(r"[^a-z0-9]")


def secret_key(email: str) -> str:
    return SYMBOL.sub("-", email.lower().replace("@", "at"))


def token_env(email: str) -> str:
    return TOKEN_PREFIX + secret_key(email).replace("-", "_")


def oauth_env(environ: Mapping[str, str]) -> dict[str, str]:
    tokens = [value for name, value in environ.items() if name.startswith(TOKEN_PREFIX)]
    return {OAUTH_ENV: tokens[0]} if len(tokens) == 1 and tokens[0] else {}
