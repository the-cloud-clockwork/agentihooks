import sys


class LateAdmission(RuntimeError):
    pass


class Released:
    def _answered(self, slug: str, request: dict, started: int, admitted: dict) -> dict:
        if self.clock() - started < 8_000:
            return admitted
        # ruleid: hub-admission-late-answer-releases-claim
        self.reopen(slug, request)
        # ruleid: hub-admission-late-answer-releases-claim
        raise LateAdmission(request["task"])


class Unclaimed:
    def _answered(self, slug: str, request: dict, started: int, admitted: dict) -> dict:
        if self.clock() - started >= 8_000:
            # ruleid: hub-admission-late-answer-releases-claim
            self._release(slug, request["task"], admitted["name"])
        return admitted


class Kept:
    def _answered(self, slug: str, request: dict, started: int, admitted: dict) -> dict:
        if self.clock() - started >= 8_000:
            # ok: hub-admission-late-answer-releases-claim
            print(f"late {request['task']}", file=sys.stderr)
        return admitted

    def reopen(self, slug: str, request: dict) -> bool:
        # ok: hub-admission-late-answer-releases-claim
        self._release(slug, request["task"], "sw-eng-1")
        return True
