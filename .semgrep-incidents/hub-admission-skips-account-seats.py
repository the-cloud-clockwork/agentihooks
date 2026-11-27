class Unchecked:
    def admit(self, slug: str, request: dict[str, str]) -> object:
        if self._unplaceable(request["hive"]):
            return {"held": "placement"}
        # ruleid: hub-admission-skips-account-seats
        return self._claim(slug, request)


class CheckedAfter:
    def admit(self, slug: str, request: dict[str, str]) -> object:
        # ruleid: hub-admission-skips-account-seats
        admitted = self._claim(slug, request)
        if reason := self._seatless(slug, request):
            return {"held": reason}
        return admitted


class Checked:
    def admit(self, slug: str, request: dict[str, str]) -> object:
        if reason := self._seatless(slug, request):
            return {"held": reason}
        # ok: hub-admission-skips-account-seats
        return self._claim(slug, request)


class CheckedPlain:
    def admit(self, slug: str, request: dict[str, str]) -> object:
        if self._seatless(slug, request):
            return {"held": "no free account seat"}
        # ok: hub-admission-skips-account-seats
        return self._claim(slug, request)


class Other:
    def reopen(self, slug: str, request: dict[str, str]) -> object:
        # ok: hub-admission-skips-account-seats
        return self._claim(slug, request)
