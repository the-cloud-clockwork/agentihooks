import os
import sys

import requests


def send(channel: str, message: str) -> bool:
    url = os.environ.get("AGENTIHOOKS_PUSH_URL")
    token = os.environ.get("AGENTIHOOKS_PUSH_TOKEN")
    if not url or not token:
        return False
    try:
        response = requests.post(
            f"{url.rstrip('/')}/api/v1/push",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel, "message": message},
            timeout=5,
        )
        response.raise_for_status()
    except requests.RequestException:
        print("incident push failed; delivery will be retried", file=sys.stderr)
        return False
    return True
