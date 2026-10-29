import os
import re
import sys
import time as time
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit

import jwt
import requests

from scripts.github_app import REFUSED as REFUSED
from scripts.github_app import GitAuthRefused as GitAuthRefused
from scripts.github_app import GitHubAppAuth as GitHubAppAuth
from scripts.swarm_v2.workspaces import identity

PROJECT = "AGENTIHOOKS_GIT_PROJECT"


def configure(project: str) -> None:
    os.environ[PROJECT] = identity(f"https://{project}").lower()
    os.environ["GIT_ASKPASS"] = str(Path(__file__).with_name("git_askpass.sh"))
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    index = int(os.environ.get("GIT_CONFIG_COUNT", "0"))
    for offset, (key, value) in enumerate((("credential.helper", ""), ("credential.useHttpPath", "true"))):
        os.environ[f"GIT_CONFIG_KEY_{index + offset}"] = key
        os.environ[f"GIT_CONFIG_VALUE_{index + offset}"] = value
    os.environ["GIT_CONFIG_COUNT"] = str(index + 2)


def askpass(prompt: str, output: TextIO) -> int:
    match = re.fullmatch(r"(Username|Password) for '(https://[^']+)': ?", prompt)
    if match is None:
        return 1
    url = urlsplit(match[2])
    project = f"{url.hostname}{url.path}".removesuffix(".git").lower()
    if url.hostname != "github.com" or url.port is not None or project != os.environ[PROJECT]:
        return 1
    value = "x-access-token" if match[1] == "Username" else GitHubAppAuth(project).get_token()
    output.write(f"{value}\n")
    return 0


def main() -> int:
    try:
        return askpass(sys.argv[1], sys.stdout)
    except (KeyError, ValueError, requests.RequestException, jwt.PyJWTError):
        print(REFUSED, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
