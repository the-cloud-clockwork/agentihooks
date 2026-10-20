import os
import time
from datetime import datetime

import jwt
import requests

from scripts.swarm_v2.workspaces import identity

REFUSED = "GitHub App cannot supply scoped contents write credentials"


class GitAuthRefused(ValueError):
    pass


class GitHubAppAuth:
    def __init__(self, project: str | None = None) -> None:
        self.project = identity(f"https://{project}").lower() if project is not None else None
        self.app_id = os.environ["GITHUB_APP_ID"]
        self.installation = os.environ["GITHUB_APP_INSTALLATION_ID"]
        self.key = os.environ["GITHUB_APP_PRIVATE_KEY"]
        self.cached: tuple[str, float] | None = None

    def get_token(self) -> str:
        now = int(time.time())
        if self.cached and now < self.cached[1] - 300:
            return self.cached[0]
        signed = jwt.encode({"iat": now - 60, "exp": now + 600, "iss": self.app_id}, self.key, algorithm="RS256")
        scope = (
            {"repositories": [self.project.rsplit("/", 1)[1]], "permissions": {"contents": "write"}}
            if self.project
            else {}
        )
        response = requests.post(
            f"https://api.github.com/app/installations/{self.installation}/access_tokens",
            headers={"Authorization": f"Bearer {signed}"},
            json=scope,
            timeout=10,
        )
        if response.status_code != 201:
            raise GitAuthRefused(REFUSED)
        data = response.json()
        if self.project:
            expected = self.project.removeprefix("github.com/")
            repositories = {repo["full_name"].lower() for repo in data["repositories"]}
            if data["permissions"].get("contents") != "write" or repositories != {expected}:
                raise GitAuthRefused(REFUSED)
        expiry = datetime.fromisoformat(data["expires_at"]).timestamp()
        self.cached = (data["token"], expiry)
        return self.cached[0]
