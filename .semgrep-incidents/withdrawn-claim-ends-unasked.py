from typing import Any, Protocol


class Apply(Protocol):
    def _apply(self, delivery: Any, prior: dict[str, str]) -> str: ...


class EndsWithdrawnUnasked(Apply):
    def _apply(self, delivery: Any, prior: dict[str, str]) -> str:
        if prior.get("state") in ("dead", "withdrawn"):
            # ruleid: withdrawn-claim-ends-unasked
            return self._ended(delivery, prior["state"])
        return "claimed"


class TermsWithdrawnUnasked(Apply):
    def _apply(self, delivery: Any, prior: dict[str, str]) -> str:
        if prior.get("state") == "withdrawn":
            # ruleid: withdrawn-claim-ends-unasked
            delivery.term()
            return "withdrawn"
        return "claimed"


class AsksWithdrawnAgain(Apply):
    def _apply(self, delivery: Any, prior: dict[str, str]) -> str:
        if prior.get("state") == "dead":
            # ok: withdrawn-claim-ends-unasked
            return self._ended(delivery)
        if prior["state"] not in ("ready", "claimed", "launching", "asked", "withdrawn"):
            # ok: withdrawn-claim-ends-unasked
            delivery.nak(30_000)
            return "busy"
        return "claimed"
