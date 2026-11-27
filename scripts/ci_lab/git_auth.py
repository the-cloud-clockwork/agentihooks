import base64
import os

from scripts.ci_lab.checks import Token

GITHUB = "https://github.com/"


def environ(auth: Token) -> dict[str, str]:
    secret = base64.b64encode(f"x-access-token:{auth.get_token()}".encode()).decode()
    return {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{GITHUB}.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {secret}",
        "GIT_TERMINAL_PROMPT": "0",
    }
