def ungated(native: str, login: str, check: bool) -> str | None:
    # ruleid: master-interactive-login-without-front-gate
    if native and login in ("", native) and check:
        return native
    return None


def gated(native: str, login: str, check: bool, front: bool) -> str | None:
    # ok: master-interactive-login-without-front-gate
    if front and native and login in ("", native) and check:
        return native
    return None
