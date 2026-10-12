import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import TextIO
from urllib.parse import urlsplit

import jwt
import requests

from scripts.swarm_v2.workspaces import identity

PROJECT = "AGENTIHOOKS_GIT_PROJECT"
REFUSED = "GitHub App cannot supply scoped contents write credentials"


class GitAuthRefused(ValueError):
    pass


class GitHubAppAuth:
    def __init__(self, project: str) -> None:
        self.project = identity(f"https://{project}").lower()
        self.app_id = os.environ["GITHUB_APP_ID"]
        self.installation = os.environ["GITHUB_APP_INSTALLATION_ID"]
        self.key = os.environ["GITHUB_APP_PRIVATE_KEY"]
        self.cached: tuple[str, float] | None = None

    def get_token(self) -> str:
        now = int(time.time())
        if self.cached and now < self.cached[1] - 300:
            return self.cached[0]
        signed = jwt.encode({"iat": now - 60, "exp": now + 600, "iss": self.app_id}, self.key, algorithm="RS256")
        response = requests.post(
            f"https://api.github.com/app/installations/{self.installation}/access_tokens",
            headers={"Authorization": f"Bearer {signed}"},
            json={"repositories": [self.project.rsplit("/", 1)[1]], "permissions": {"contents": "write"}},
            timeout=10,
        )
        if response.status_code != 201:
            raise GitAuthRefused(REFUSED)
        data = response.json()
        expected = self.project.removeprefix("github.com/")
        repositories = {repo["full_name"].lower() for repo in data["repositories"]}
        if data["permissions"].get("contents") != "write" or repositories != {expected}:
            raise GitAuthRefused(REFUSED)
        self.cached = (data["token"], datetime.fromisoformat(data["expires_at"]).timestamp())
        return self.cached[0]


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
