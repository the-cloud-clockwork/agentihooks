from typing import Any


class Ungated:
    def place_master(self, native: str, login: str, check: bool, front: bool) -> Any:
        # ruleid: master-interactive-login-without-front-gate
        if native and login in ("", native) and check:
            return native
        return None


class Gated:
    def place_master(self, native: str, login: str, check: bool, front: bool) -> Any:
        # ok: master-interactive-login-without-front-gate
        if front and native and login in ("", native) and check:
            return native
        return None
