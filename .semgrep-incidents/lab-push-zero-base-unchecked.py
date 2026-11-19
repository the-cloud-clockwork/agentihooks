from collections.abc import Mapping
from typing import Any

from scripts.ci_lab.webhooks import HeadEvent


def unchecked(payload: Mapping[str, Any]) -> list[HeadEvent]:
    repo, actor = payload["repository"]["html_url"], payload["sender"]["login"]
    # ruleid: lab-push-zero-base-unchecked
    return [HeadEvent("push", repo, payload["after"], payload["before"], actor, "pushed")]


def checked(payload: Mapping[str, Any]) -> list[HeadEvent]:
    if payload["before"] == "0" * 40:
        return []
    repo, actor = payload["repository"]["html_url"], payload["sender"]["login"]
    # ok: lab-push-zero-base-unchecked
    return [HeadEvent("push", repo, payload["after"], payload["before"], actor, "pushed")]
