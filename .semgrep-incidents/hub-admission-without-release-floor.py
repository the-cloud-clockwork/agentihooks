class Admission:
    # ruleid: hub-admission-without-release-floor
    def admit(self, slug, request):
        if self._stopped(slug):
            return None
        if not self._placeable(request["hive"]):
            return None
        return self._claim(slug, request)


class Floored:
    class Admission:
        # ok: hub-admission-without-release-floor
        def admit(self, slug, request):
            if self._stopped(slug):
                return None
            if older := self._outdated(request["hive"]):
                print(older)
                return None
            return self._claim(slug, request)


class Quota:
    # ok: hub-admission-without-release-floor
    def admit(self, account, harness):
        return self._room(account, harness)
