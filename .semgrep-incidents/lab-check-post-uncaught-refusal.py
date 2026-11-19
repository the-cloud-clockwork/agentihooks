from typing import Any

Moved = list[tuple[str, str, str | None]]


class GitAuthRefused(ValueError):
    pass


class PostRefused(RuntimeError):
    pass


class Unguarded:
    def _post(self, state: Any, moved: Moved) -> list[str]:
        self: Any
        poster = self.ports.checks(state.repo)
        for check, status, conclusion in moved:
            # ruleid: lab-check-post-uncaught-refusal
            state.runs[check] = poster.post(state.head, check, status, conclusion, state.output, False)
            state.posted[check] = [status, conclusion]
        return [f"posted {check} {status} on {state.head}" for check, status, _ in moved]


class GuardsOnlyTheToken:
    def _post(self, state: Any, moved: Moved) -> list[str]:
        self: Any
        poster = self.ports.checks(state.repo)
        actions: list[str] = []
        for check, status, conclusion in moved:
            try:
                # ruleid: lab-check-post-uncaught-refusal
                state.runs[check] = poster.post(state.head, check, status, conclusion, state.output, False)
            except GitAuthRefused as error:
                actions.append(f"held {check} {status} on {state.head} for a later pass: {error}")
                break
            state.posted[check] = [status, conclusion]
            actions.append(f"posted {check} {status} on {state.head}")
        return actions


class Guarded:
    def _post(self, state: Any, moved: Moved) -> list[str]:
        self: Any
        poster = self.ports.checks(state.repo)
        actions: list[str] = []
        for check, status, conclusion in moved:
            try:
                # ok: lab-check-post-uncaught-refusal
                state.runs[check] = poster.post(state.head, check, status, conclusion, state.output, False)
            except (GitAuthRefused, PostRefused, OSError) as error:
                actions.append(f"held {check} {status} on {state.head} for a later pass: {error}")
                break
            state.posted[check] = [status, conclusion]
            actions.append(f"posted {check} {status} on {state.head}")
        return actions
