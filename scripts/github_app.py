import os
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import jwt
import requests

from scripts.swarm_v2.workspaces import identity

REFUSED = "GitHub App cannot supply scoped contents write credentials"


class GitAuthRefused(ValueError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GitHubAppAuth:
    def __init__(self, project: str | None = None, *, permissions: Mapping[str, str] | None = None) -> None:
        self.project = identity(f"https://{project}").lower() if project is not None else None
        self.permissions = (
            dict(permissions) if permissions is not None else {"contents": "write"} if project is not None else None
        )
        self.app_id = os.environ["GITHUB_APP_ID"]
        self.installation = os.environ["GITHUB_APP_INSTALLATION_ID"]
        self.key = os.environ["GITHUB_APP_PRIVATE_KEY"]
        self.cached: tuple[str, float] | None = None

    def get_token(self) -> str:
        now = int(time.time())
        if self.cached is not None and now < self.cached[1] - 300:
            return self.cached[0]
        signed = jwt.encode({"iat": now - 60, "exp": now + 600, "iss": self.app_id}, self.key, algorithm="RS256")
        response = requests.post(
            f"https://api.github.com/app/installations/{self.installation}/access_tokens",
            headers={"Authorization": f"Bearer {signed}"},
            json=self.scope(),
            timeout=10,
        )
        if response.status_code != 201:
            raise GitAuthRefused(REFUSED, status_code=response.status_code)
        data = response.json()
        if not self.granted(data):
            raise GitAuthRefused(REFUSED)
        expiry = datetime.fromisoformat(data["expires_at"]).timestamp()
        self.cached = (data["token"], expiry)
        return self.cached[0]

    def scope(self) -> dict[str, list[str] | dict[str, str]]:
        """Return the token request body: the project's repository and the requested permissions, if any."""
        scope: dict[str, list[str] | dict[str, str]] = {}
        if self.project:
            scope["repositories"] = [self.project.rsplit("/", 1)[1]]
        if self.permissions is not None:
            scope["permissions"] = self.permissions
        return scope

    def granted(self, data: dict[str, Any]) -> bool:
        """Return whether the minted token holds the project's repository alone and every requested permission."""
        if self.project:
            expected = self.project.removeprefix("github.com/")
            if {repo["full_name"].lower() for repo in data["repositories"]} != {expected}:
                return False
        permissions = self.permissions or {}
        return all(data["permissions"].get(key) == value for key, value in permissions.items())
