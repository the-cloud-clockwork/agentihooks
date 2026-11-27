import base64


def lowercase(url: str, token: str) -> dict[str, str]:
    secret = base64.b64encode(f"lab:{token}".encode()).decode()
    # ruleid: lab-remote-basic-scheme
    return {"GIT_CONFIG_KEY_0": f"http.{url}.extraheader", "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {secret}"}


def shouting(secret: str) -> str:
    # ruleid: lab-remote-basic-scheme
    return f"Authorization: BASIC {secret}"


def header_map(secret: str) -> dict[str, str]:
    # ruleid: lab-remote-basic-scheme
    return {"Authorization": f"basic {secret}"}


def header_item(headers: dict[str, str], secret: str) -> None:
    # ruleid: lab-remote-basic-scheme
    headers["authorization"] = "basic " + secret


def header_pair(secret: str) -> tuple[str, str]:
    # ruleid: lab-remote-basic-scheme
    return ("Authorization", f"basic {secret}")


def canonical(url: str, token: str) -> dict[str, str]:
    secret = base64.b64encode(f"lab:{token}".encode()).decode()
    # ok: lab-remote-basic-scheme
    return {"GIT_CONFIG_KEY_0": f"http.{url}.extraheader", "GIT_CONFIG_VALUE_0": f"Authorization: Basic {secret}"}


def canonical_map(secret: str) -> dict[str, str]:
    # ok: lab-remote-basic-scheme
    return {"Authorization": f"Basic {secret}"}


def bearer(token: str) -> str:
    # ok: lab-remote-basic-scheme
    return f"Authorization: Bearer {token}"
