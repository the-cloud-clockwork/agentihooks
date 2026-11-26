import os

from scripts.ci_lab.checks import Token

GITHUB = "https://github.com/"


def environ(auth: Token) -> dict[str, str]:
    return dict(os.environ)
