class Skips:
    def _intake(self, now_ms: int) -> list[str]:
        return [f"received at {now_ms}"]

    # ruleid: lab-pass-skips-merge-group-retire
    def tick(self, now_ms: int) -> list[str]:
        actions = [*self._intake(now_ms)]
        return actions


class Mentions:
    def _intake(self, now_ms: int) -> list[str]:
        return [f"received at {now_ms}"]

    # ruleid: lab-pass-skips-merge-group-retire
    def tick(self, now_ms: int) -> list[str]:
        """Run one pass without self._retire_groups()."""
        actions = [*self._intake(now_ms)]
        return actions


class RetiresLast:
    def _retire_groups(self) -> list[str]:
        return []

    def _intake(self, now_ms: int) -> list[str]:
        return [f"received at {now_ms}"]

    # ruleid: lab-pass-skips-merge-group-retire
    def tick(self, now_ms: int) -> list[str]:
        actions = [*self._intake(now_ms), *self._retire_groups()]
        return actions


class Retires:
    def _retire_groups(self) -> list[str]:
        return []

    def _intake(self, now_ms: int) -> list[str]:
        return [f"received at {now_ms}"]

    # ok: lab-pass-skips-merge-group-retire
    def tick(self, now_ms: int) -> list[str]:
        """Run one pass."""
        actions = [*self._retire_groups(), *self._intake(now_ms)]
        return actions
