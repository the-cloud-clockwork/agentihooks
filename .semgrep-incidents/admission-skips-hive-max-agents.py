import threading


class Unchecked:
    def admit(self, slug: str, request: dict[str, str]) -> object:
        if self._unplaceable(request["hive"]):
            return None
        # ruleid: admission-skips-hive-max-agents
        return self._claim(slug, request)


class CheckedAfter:
    def admit(self, slug: str, request: dict[str, str]) -> object:
        # ruleid: admission-skips-hive-max-agents
        admitted = self._claim(slug, request)
        if self._full(request["hive"]):
            return None
        return admitted


class CheckedUnderLock:
    def admit(self, slug: str, request: dict[str, str]) -> object:
        with threading.Lock():
            if full := self._full(request["hive"]):
                return {"held": full}
            # ok: admission-skips-hive-max-agents
            admitted = self._claim(slug, request)
        return admitted


class Checked:
    def admit(self, slug: str, request: dict[str, str]) -> object:
        if full := self._full(request["hive"]):
            print(full)
            return None
        # ok: admission-skips-hive-max-agents
        return self._claim(slug, request)


class CheckedPlain:
    def admit(self, slug: str, request: dict[str, str]) -> object:
        if self._full(request["hive"]):
            return None
        # ok: admission-skips-hive-max-agents
        admitted = self._claim(slug, request)
        return admitted


class Other:
    def reopen(self, slug: str, request: dict[str, str]) -> object:
        # ok: admission-skips-hive-max-agents
        return self._claim(slug, request)
