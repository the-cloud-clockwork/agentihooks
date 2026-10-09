import os
import sys


def send(channel: str, message: str) -> bool:
    import requests

    url = os.environ.get("AGENTIHOOKS_PUSH_URL")
    token = os.environ.get("AGENTIHOOKS_PUSH_TOKEN")
    if not url or not token:
        return False
    endpoint = url.rstrip("/")
    if not endpoint.endswith("/api/v1/push"):
        endpoint = f"{endpoint}/api/v1/push"
    try:
        response = requests.post(
            endpoint,
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel, "message": message},
            timeout=5,
        )
        response.raise_for_status()
    except requests.RequestException:
        print("incident push failed; delivery will be retried", file=sys.stderr)
        return False
    return True
