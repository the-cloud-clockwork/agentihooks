class Unchecked:
    def admit(self, slug: str, request: dict[str, str]) -> object:
        if self._unplaceable(request["hive"]):
            return None
        # ruleid: admission-skips-hive-max-agents
        return self._claim(slug, request)


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
