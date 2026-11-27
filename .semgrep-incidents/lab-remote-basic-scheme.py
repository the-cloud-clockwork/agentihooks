import base64


def lowercase(url: str, token: str) -> dict[str, str]:
    secret = base64.b64encode(f"lab:{token}".encode()).decode()
    # ruleid: lab-remote-basic-scheme
    return {"GIT_CONFIG_KEY_0": f"http.{url}.extraheader", "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {secret}"}


def shouting(secret: str) -> str:
    # ruleid: lab-remote-basic-scheme
    return f"Authorization: BASIC {secret}"


def canonical(url: str, token: str) -> dict[str, str]:
    secret = base64.b64encode(f"lab:{token}".encode()).decode()
    # ok: lab-remote-basic-scheme
    return {"GIT_CONFIG_KEY_0": f"http.{url}.extraheader", "GIT_CONFIG_VALUE_0": f"Authorization: Basic {secret}"}


def bearer(token: str) -> str:
    # ok: lab-remote-basic-scheme
    return f"Authorization: Bearer {token}"
