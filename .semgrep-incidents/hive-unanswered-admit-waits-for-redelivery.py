from scripts.hive import admission


class Skipped:
    def _return_offer(self, event: object, error: Exception) -> None:
        # ruleid: hive-unanswered-admit-waits-for-redelivery
        if not isinstance(error, admission.Unanswered):
            self.tasks.reopen(event)


class Inline:
    def _apply(self, delivery: object, event: object, revision: int) -> str:
        try:
            return self._spawn(event)
        except Exception as error:
            self._clear_pending(revision)
            # ruleid: hive-unanswered-admit-waits-for-redelivery
            if not isinstance(error, admission.Unanswered):
                self.tasks.reopen(event)
            raise


class Asked:
    def _apply(self, delivery: object, event: object, revision: int) -> str:
        try:
            return self._spawn(event)
        except admission.Unanswered:
            delivery.nak(30_000)
            return "asked"
        except Exception:
            self._clear_pending(revision)
            # ok: hive-unanswered-admit-waits-for-redelivery
            self.tasks.reopen(event)
            raise
