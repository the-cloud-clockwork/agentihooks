from typing import Any


class MoveRefused(ValueError):
    pass


class GitAuthRefused(ValueError):
    pass


class PostRefused(RuntimeError):
    pass


class Unguarded:
    def _fail(self, head: str, output: Any) -> list[str]:
        self: Any
        # ruleid: lab-fail-post-uncaught-move-refusal
        self.ports.checks(self.repo).post(head, "required", "completed", "failure", output, True)
        return []


class GuardsOnlyTheOldRefusals:
    def _fail(self, head: str, output: Any) -> list[str]:
        self: Any
        try:
            # ruleid: lab-fail-post-uncaught-move-refusal
            self.ports.checks(self.repo).post(head, "required", "completed", "failure", output, True)
        except (GitAuthRefused, PostRefused, OSError) as refusal:
            return [f"cannot post the failed required check on {head}: {refusal}"]
        return []


class Guarded:
    def _fail(self, head: str, output: Any) -> list[str]:
        self: Any
        try:
            # ok: lab-fail-post-uncaught-move-refusal
            self.ports.checks(self.repo).post(head, "required", "completed", "failure", output, True)
        except (GitAuthRefused, PostRefused, OSError) as refusal:
            return [f"cannot post the failed required check on {head}: {refusal}"]
        except MoveRefused:
            return ["the failed required check was already posted in this pass"]
        return []


class OtherMethod:
    def _post(self, head: str, output: Any) -> list[str]:
        self: Any
        # ok: lab-fail-post-uncaught-move-refusal
        self.ports.checks(self.repo).post(head, "required", "completed", "failure", output, True)
        return []
