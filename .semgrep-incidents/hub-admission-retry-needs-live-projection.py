from typing import Any


class Admission:
    def admit(self, slug: str, admitted: dict[str, Any]) -> dict[str, Any] | None:
        # ruleid: hub-admission-retry-needs-live-projection
        live = admitted["name"] in self.store.listed(slug)
        if live:
            return admitted
        # ok: hub-admission-retry-needs-live-projection
        if self._active(slug, admitted):
            return admitted
        return None

    def _kept(self, slug: str, admitted: dict[str, Any]) -> dict[str, Any] | None:
        # ruleid: hub-admission-retry-needs-live-projection
        if admitted["name"] in self.store.listed(slug):
            return admitted
        # ok: hub-admission-retry-needs-live-projection
        if self._active(slug, admitted):
            return admitted
        return None

    def _active(self, slug: str, admitted: dict[str, Any]) -> bool:
        # ok: hub-admission-retry-needs-live-projection
        if admitted["name"] in self.store.listed(slug):
            return True
        return self.current_starting_claimant(slug, admitted)
