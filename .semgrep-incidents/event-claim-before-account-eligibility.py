from typing import Any, Protocol


class HubAdmission(Protocol):
    def admit(self, offer_quota: Any) -> Any: ...


class MissingHubCheck(HubAdmission):
    def admit(self: Any, offer_quota: Any) -> Any:
        # ruleid: event-claim-before-account-eligibility
        return self._claim()


class LateHubCheck(HubAdmission):
    def admit(self: Any, offer_quota: Any) -> Any:
        # ruleid: event-claim-before-account-eligibility
        claimed = self._claim()
        if not offer_quota.available():
            return {"held": "no account seat"}
        return claimed


class CheckedHub(HubAdmission):
    def admit(self: Any, offer_quota: Any) -> Any:
        if not offer_quota.available():
            return {"held": "no account seat"}
        # ok: event-claim-before-account-eligibility
        return self._claim()


class CheckedHubHold(HubAdmission):
    def admit(self: Any, offer_quota: Any) -> Any:
        with self._turn():
            if full := self._full() or self._account_hold():
                return {"held": full}
            # ok: event-claim-before-account-eligibility
            return self._claim()


class MissingSpokeCheck:
    def _poll(self: Any) -> Any:
        # ruleid: event-claim-before-account-eligibility
        return self._apply()


class LateSpokeCheck:
    def _poll(self: Any, offer_quota: Any) -> Any:
        # ruleid: event-claim-before-account-eligibility
        claimed = self._apply()
        fit = offer_quota.Fit()
        return claimed, fit


class CheckedSpoke:
    def _poll(self: Any, offer_quota: Any) -> Any:
        fit = offer_quota.Fit()
        # ok: event-claim-before-account-eligibility
        return self._apply(fit)
