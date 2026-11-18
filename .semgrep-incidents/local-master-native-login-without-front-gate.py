MASTER = "master"


def holds(slug: str, seat: str) -> bool:
    return seat in ("", f"master@{slug}")


def ungated(lane: str, harness: str, task: dict) -> str:
    explicit = task.get("route_kind")
    # ruleid: local-master-native-login-without-front-gate
    if explicit or lane != MASTER or harness not in ("", "claude"):
        return explicit or "subscription"
    return "interactive"


def gated(lane: str, harness: str, task: dict, slug: str) -> str:
    explicit = task.get("route_kind")
    # ok: local-master-native-login-without-front-gate
    if explicit or lane != MASTER or harness not in ("", "claude") or not holds(slug, task.get("seat", "")):
        return explicit or "subscription"
    return "interactive"
