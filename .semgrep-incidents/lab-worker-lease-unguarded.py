import http.client
from typing import Any, Protocol


class Leasing(Protocol):
    def run_once(self) -> list[Any]: ...


class Unsafe(Leasing):
    def run_once(self) -> list[Any]:
        self: Any
        # ruleid: lab-worker-lease-unguarded
        leases = self.client.lease(self.name, 1, self.capabilities, self.wait_ms)
        return list(leases)


class Safe(Leasing):
    def run_once(self) -> list[Any]:
        self: Any
        try:
            # ok: lab-worker-lease-unguarded
            leases = self.client.lease(self.name, 1, self.capabilities, self.wait_ms)
        except (OSError, http.client.HTTPException) as error:
            self._back_off(error)
            return []
        return list(leases)
